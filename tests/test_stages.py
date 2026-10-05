from __future__ import annotations

from copy import deepcopy

import pytest

from multi_hazard_pipeline.agents.classification_agent import classification_agent
from multi_hazard_pipeline.agents.review_agent import deterministic_candidate_evaluation, review_agent
from multi_hazard_pipeline.agents.segment_agent import (
    _restore_consolidated_evidence,
    consolidate_exact_duplicates,
    segment_agent,
    stabilize_segment_ids,
)
from multi_hazard_pipeline.config import DEFAULT_CONFIG
from multi_hazard_pipeline.schemas import classification_response_schema, validate_review_payload
from multi_hazard_pipeline.schemas import validate_segment_chain


def source() -> dict:
    return {
        "doc_id": "report",
        "chunks": [
            {
                "chunk_id": "c1",
                "doc_id": "document-1",
                "filename": "report.txt",
                "source_type": "txt",
                "text": "Heavy rainfall mobilized sediment into the channel.",
            },
            {
                "chunk_id": "c2",
                "doc_id": "document-1",
                "filename": "report.txt",
                "source_type": "txt",
                "text": "The bridge became blocked by debris.",
            },
        ],
    }


def segment(segment_id: int, order: int, process: str, chunk_id: str, quote: str, predecessors=None):
    return {
        "segment": segment_id,
        "causal_order": order,
        "predecessor_segment_ids": predecessors or [],
        "event": "Report catchment",
        "process": process,
        "evidence": [{"chunk_id": chunk_id, "quote": quote}],
    }


def chain() -> dict:
    return {
        "doc_id": "report",
        "segments": [
            segment(1, 1, "Rainfall mobilized sediment", "c1", "Heavy rainfall mobilized sediment"),
            segment(2, 2, "Bridge became blocked", "c2", "bridge became blocked by debris", [1]),
        ],
    }


def classification_row(item: dict, category="Material Mobilization") -> dict:
    return deepcopy(item) | {
        "generalized_category": category,
        "interaction_type": "Process-process",
        "sediment_transport_phase": "Erosion",
        "classification_rationale": ["Rule applied"],
    }


def consolidation_row(item: dict, source_ids: list[int]) -> dict:
    return {key: deepcopy(item[key]) for key in ("segment", "causal_order", "predecessor_segment_ids", "event", "process")} | {"source_segment_ids": source_ids}


class PayloadClient:
    def __init__(self, payloads):
        self.payloads = list(payloads)
        self.calls = []

    def complete_json(self, **kwargs):
        self.calls.append(kwargs)
        payload = deepcopy(self.payloads.pop(0))
        kwargs["validate"](payload)
        return payload


def test_invalid_evidence_chunk_id_is_rejected() -> None:
    payload = {"segments": [segment(1, 1, "Process", "missing", "quote")]}
    with pytest.raises(ValueError, match="unknown chunk"):
        validate_segment_chain(payload, source())


def test_evidence_quote_absent_from_chunk_is_rejected() -> None:
    payload = {"segments": [segment(1, 1, "Process", "c1", "not present")]}
    with pytest.raises(ValueError, match="quote not found.*'not present'.*literal substring"):
        validate_segment_chain(payload, source())


def test_nonliteral_cross_page_quote_gets_semantically_verified_exact_fragments() -> None:
    two_pages = {
        "doc_id": "report",
        "chunks": [
            {"chunk_id": "p1", "filename": "report.pdf", "text": "Heavy rainfall mobilized"},
            {"chunk_id": "p2", "filename": "report.pdf", "text": "sediment into the channel."},
        ],
    }
    proposed = {"segments": [segment(1, 1, "Rainfall mobilized sediment", "p2", "Heavy rainfall mobilized sediment into the channel.")]}
    client = PayloadClient([proposed, {"supported": True, "fragment_ids": ["f0-0", "f1-0"], "reason": "Together they state the claim."}])
    result = segment_agent(client, two_pages, DEFAULT_CONFIG)
    assert [citation["quote"] for citation in result["segments"][0]["evidence"]] == [
        "Heavy rainfall mobilized", "sediment into the channel.",
    ]
    assert [call["response_schema"]["name"] for call in client.calls] == [
        "causal_segments", "citation_semantic_verification",
    ]


def test_nonliteral_quote_is_rejected_when_semantic_check_fails() -> None:
    proposed = {"segments": [segment(1, 1, "Unsupported process", "c1", "not present")]}
    client = PayloadClient([proposed, {"supported": False, "fragment_ids": [], "reason": "No support."}])
    with pytest.raises(ValueError, match="not supported"):
        segment_agent(client, source(), DEFAULT_CONFIG)


@pytest.mark.parametrize(
    "payload,message",
    [
        ({"segments": [segment(1, 1, "A", "c1", "Heavy rainfall"), segment(1, 2, "B", "c2", "bridge became blocked")]}, "unique"),
        ({"segments": [segment(1, 2, "A", "c1", "Heavy rainfall")]}, "causal_order"),
        ({"segments": [segment(1, 1, "A", "c1", "Heavy rainfall", [2])]}, "unknown predecessor"),
        ({"segments": [segment(1, 1, "A", "c1", "Heavy rainfall", [1])]}, "not earlier"),
    ],
)
def test_segment_ids_order_and_predecessors_are_validated(payload, message) -> None:
    with pytest.raises(ValueError, match=message):
        validate_segment_chain(payload, source())


def test_cross_batch_exact_duplicates_preserve_all_evidence() -> None:
    items = [
        segment(1, 1, "Sediment moved downstream", "c1", "Heavy rainfall mobilized sediment"),
        segment(2, 2, "Sediment moved downstream", "c2", "bridge became blocked by debris"),
    ]
    consolidated = consolidate_exact_duplicates(items)
    assert len(consolidated) == 1
    assert consolidated[0]["segment"] == 1
    assert {item["chunk_id"] for item in consolidated[0]["evidence"]} == {"c1", "c2"}


def test_unchanged_steps_keep_ids_across_segmentation_revision() -> None:
    previous = chain()
    revised = {
        "doc_id": "report",
        "segments": [
            segment(1, 1, "New triggering step", "c1", "Heavy rainfall"),
            segment(2, 2, "Rainfall mobilized sediment", "c1", "mobilized sediment", [1]),
            segment(3, 3, "Bridge became blocked", "c2", "bridge became blocked", [2]),
        ],
    }
    stable = stabilize_segment_ids(revised, previous)
    assert [item["segment"] for item in stable["segments"]] == [3, 1, 2]
    assert stable["segments"][2]["predecessor_segment_ids"] == [1]


def test_stable_id_gap_after_deletion_is_valid() -> None:
    previous = chain()
    revised = {
        "doc_id": "report",
        "segments": [
            segment(1, 1, "Rainfall mobilized sediment", "c1", "mobilized sediment"),
            segment(2, 2, "Bridge became blocked", "c2", "bridge became blocked", [1]),
        ],
    }
    previous["segments"][1]["segment"] = 3
    stable = stabilize_segment_ids(revised, previous)
    rows = [classification_row(item) for item in stable["segments"]]
    checks, issues = deterministic_candidate_evaluation({"rows": rows}, source(), DEFAULT_CONFIG)
    assert [item["segment"] for item in rows] == [1, 3]
    assert checks["causal_chain_coherent"] is True
    assert not any(issue["code"] == "invalid_causal_order" for issue in issues)


def test_multiple_batches_receive_a_final_consolidation_call() -> None:
    preliminary_1 = {"segments": [segment(1, 1, "Rainfall mobilized sediment", "c1", "Heavy rainfall mobilized sediment")]}
    preliminary_2 = {"segments": [segment(1, 1, "Bridge became blocked", "c2", "bridge became blocked by debris")]}
    consolidated = {"segments": [consolidation_row(item, [item["segment"]]) for item in chain()["segments"]]}
    client = PayloadClient([preliminary_1, preliminary_2, consolidated])
    from dataclasses import replace

    result = segment_agent(client, source(), replace(DEFAULT_CONFIG, batch_max_chars=60))
    assert len(client.calls) == 3
    assert "Consolidate" in client.calls[-1]["system_prompt"]
    assert [item["segment"] for item in result["segments"]] == [1, 2]
    assert result["segments"][1]["evidence"] == chain()["segments"][1]["evidence"]


def test_segmentation_correction_uses_smaller_batches() -> None:
    from dataclasses import replace

    first = {"segments": [segment(1, 1, "Rainfall mobilized sediment", "c1", "Heavy rainfall mobilized sediment")]}
    second = {"segments": [segment(1, 1, "Bridge became blocked", "c2", "bridge became blocked by debris")]}
    consolidated = {"segments": [consolidation_row(item, [item["segment"]]) for item in chain()["segments"]]}
    client = PayloadClient([first, second, consolidated])
    result = segment_agent(client, source(), replace(DEFAULT_CONFIG, batch_max_chars=120), correction_instruction="Fix the chain.")

    assert [len(call["user_payload"].get("chunks", [])) for call in client.calls] == [1, 1, 0]
    assert len(result["segments"]) == 2


def test_consolidation_error_names_the_missing_citation() -> None:
    preliminary_1 = {"segments": [segment(1, 1, "Rainfall mobilized sediment", "c1", "Heavy rainfall mobilized sediment")]}
    preliminary_2 = {"segments": [segment(1, 1, "Bridge became blocked", "c2", "bridge became blocked by debris")]}
    client = PayloadClient([preliminary_1, preliminary_2, {"segments": [consolidation_row(preliminary_1["segments"][0], [1])]}])
    from dataclasses import replace

    with pytest.raises(ValueError, match="bridge became blocked by debris"):
        segment_agent(client, source(), replace(DEFAULT_CONFIG, batch_max_chars=60))


def test_consolidation_rejects_reused_source_segment() -> None:
    preliminary_1 = {"segments": [segment(1, 1, "Rainfall mobilized sediment", "c1", "Heavy rainfall mobilized sediment")]}
    preliminary_2 = {"segments": [segment(1, 1, "Bridge became blocked", "c2", "bridge became blocked by debris")]}
    rows = [consolidation_row(item, [item["segment"]]) for item in chain()["segments"]]
    rows[1]["source_segment_ids"] = [1, 2]
    client = PayloadClient([preliminary_1, preliminary_2, {"segments": rows}])
    from dataclasses import replace

    with pytest.raises(ValueError, match="more than once"):
        segment_agent(client, source(), replace(DEFAULT_CONFIG, batch_max_chars=60))


def test_consolidation_merge_keeps_every_original_citation() -> None:
    merged = consolidation_row(chain()["segments"][0], [1, 2])
    result = _restore_consolidated_evidence({"segments": [merged]}, chain()["segments"], source())
    assert result[0]["evidence"] == [
        chain()["segments"][0]["evidence"][0],
        chain()["segments"][1]["evidence"][0],
    ]


def test_classification_requires_exactly_one_result_per_segment() -> None:
    duplicate = classification_row(chain()["segments"][0])
    client = PayloadClient([{"rows": [duplicate, duplicate]}])
    with pytest.raises(ValueError, match="more than once"):
        classification_agent(client, chain(), DEFAULT_CONFIG)


def test_classification_cannot_rewrite_segment_fields() -> None:
    rows = [classification_row(item) for item in chain()["segments"]]
    rows[0]["process"] = "LLM rewrite"
    client = PayloadClient([{"rows": rows}])
    with pytest.raises(ValueError, match="rewrote.*process"):
        classification_agent(client, chain(), DEFAULT_CONFIG)


def test_compact_classification_preserves_source_segments() -> None:
    rows = [
        {key: value for key, value in classification_row(item).items()
         if key in {"segment", "generalized_category", "interaction_type", "sediment_transport_phase", "classification_rationale"}}
        for item in chain()["segments"]
    ]
    client = PayloadClient([{"rows": rows}])
    result = classification_agent(client, chain(), DEFAULT_CONFIG)
    assert result["rows"] == [classification_row(item) for item in chain()["segments"]]
    schema_row = classification_response_schema(DEFAULT_CONFIG)["schema"]["properties"]["rows"]["items"]
    assert set(schema_row["properties"]) == set(rows[0])


def test_classification_rejects_unknown_controlled_labels() -> None:
    rows = [classification_row(item) for item in chain()["segments"]]
    rows[0]["generalized_category"] = "Ordinary social benefit"
    client = PayloadClient([{"rows": rows}])
    with pytest.raises(ValueError, match="invalid generalized_category"):
        classification_agent(client, chain(), DEFAULT_CONFIG)


def test_targeted_classification_preserves_unaffected_rows() -> None:
    existing = {"doc_id": "report", "rows": [classification_row(item) for item in chain()["segments"]]}
    replacement = classification_row(chain()["segments"][1], "Negative Impact on permanent or temporary infrastructure")
    replacement["interaction_type"] = "Process-structure"
    replacement["sediment_transport_phase"] = "Dysconnectivity"
    client = PayloadClient([{"rows": [replacement]}])
    result = classification_agent(client, chain(), DEFAULT_CONFIG, segment_ids=[2], existing=existing)
    assert result["rows"][0] == existing["rows"][0]
    assert result["rows"][1]["generalized_category"].startswith("Negative Impact")


def test_whole_report_evaluator_rejects_bad_evidence_before_llm() -> None:
    rows = [classification_row(item) for item in chain()["segments"]]
    rows[0]["evidence"][0]["chunk_id"] = "missing"
    candidate = {"doc_id": "report", "rows": rows}
    client = PayloadClient([])
    result = review_agent(client, candidate, source(), DEFAULT_CONFIG)
    assert result["status"] == "revision_required"
    assert result["evaluation_source"] == "deterministic"
    assert client.calls == []
    assert result["checks"]["evidence_verified"] is False


def test_whole_report_evaluator_receives_complete_candidate_and_source() -> None:
    rows = [classification_row(item) for item in chain()["segments"]]
    candidate = {"doc_id": "report", "rows": rows}
    evaluation = {
        "status": "pass",
        "summary": "The complete chain is coherent.",
        "issues": [],
        "checks": {
            "causal_chain_coherent": True,
            "evidence_verified": True,
            "segments_complete": True,
            "segments_unique": True,
            "categories_valid": True,
        },
    }
    client = PayloadClient([evaluation])
    bilingual = source()
    for chunk in bilingual["chunks"]:
        chunk["source_language"] = "English"
        chunk["translated_text"] = chunk["text"]
    result = review_agent(client, candidate, bilingual, DEFAULT_CONFIG)
    assert result["status"] == "pass"
    assert client.calls[0]["user_payload"]["candidate_report"] == candidate
    assert client.calls[0]["user_payload"]["source_chunks"] == bilingual["chunks"]
    assert '"segment_ids":[1]' in client.calls[0]["system_prompt"]
    assert '"segment_ids":[]' in client.calls[0]["system_prompt"]
    assert "neutral fallback for pre-event conditions" in client.calls[0]["system_prompt"]
    assert "do not call it\nNegative Impact merely because" in client.calls[0]["system_prompt"]


@pytest.mark.parametrize("stage,check", [
    ("categorization", "categories_valid"),
    ("segmentation", "causal_chain_coherent"),
    ("translation", "evidence_verified"),
])
def test_review_reconciles_issue_check_without_approving(stage: str, check: str) -> None:
    candidate = {"doc_id": "report", "rows": [classification_row(item) for item in chain()["segments"]]}
    payload = {
        "status": "revision_required",
        "summary": "Needs review.",
        "issues": [{"stage": stage, "segment_ids": [1], "code": "wrong_step", "message": "Review segment 1.", "suggested_action": "Check it."}],
        "checks": dict.fromkeys(("causal_chain_coherent", "evidence_verified", "segments_complete", "segments_unique", "categories_valid"), True),
    }
    result = review_agent(PayloadClient([payload]), candidate, source(), DEFAULT_CONFIG)
    assert result["status"] == "revision_required"
    assert result["checks"][check] is False
    assert result["issues"] == payload["issues"]


def test_categorization_evaluation_requires_affected_segment_ids() -> None:
    payload = {
        "status": "revision_required",
        "summary": "A category is wrong.",
        "issues": [
            {
                "stage": "categorization",
                "segment_ids": [],
                "code": "wrong_category",
                "message": "A category is wrong.",
                "suggested_action": "Reclassify the affected segment.",
            }
        ],
        "checks": {
            "causal_chain_coherent": True,
            "evidence_verified": True,
            "segments_complete": True,
            "segments_unique": True,
            "categories_valid": False,
        },
    }
    with pytest.raises(ValueError, match="identify affected segment IDs"):
        validate_review_payload(payload)
