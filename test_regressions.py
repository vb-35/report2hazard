"""Small dependency-free regression smoke check; the full suite lives in tests/."""

from multi_hazard_pipeline.agents.classification_agent import classification_agent
from multi_hazard_pipeline.agents.review_agent import review_agent
from multi_hazard_pipeline.config import DEFAULT_CONFIG


def segment(number: int) -> dict:
    return {
        "segment": number,
        "causal_order": number,
        "predecessor_segment_ids": [] if number == 1 else [number - 1],
        "event": "event",
        "process": f"process {number}",
        "evidence": [{"chunk_id": "c1", "quote": "source evidence"}],
    }


def row(number: int) -> dict:
    return segment(number) | {
        "generalized_category": "Triggering Event",
        "interaction_type": "Process-process",
        "sediment_transport_phase": "Erosion",
        "classification_rationale": ["reason"],
    }


class ClassificationClient:
    rejected = 0

    def complete_json(self, **kwargs):
        validate = kwargs["validate"]
        for invalid in ({"rows": [row(1)]}, {"rows": [row(1), row(1)]}):
            try:
                validate(invalid)
            except ValueError:
                self.rejected += 1
            else:
                raise AssertionError("incomplete or duplicate classifications were accepted")
        payload = {"rows": [row(1), row(2)]}
        validate(payload)
        return payload


class ReviewClient:
    def complete_json(self, **kwargs):
        assert "complete candidate report" in kwargs["system_prompt"]
        assert len(kwargs["user_payload"]["candidate_report"]["rows"]) == 2
        payload = {
            "status": "pass",
            "summary": "Complete report is coherent.",
            "issues": [],
            "checks": {
                "causal_chain_coherent": True,
                "evidence_verified": True,
                "segments_complete": True,
                "segments_unique": True,
                "categories_valid": True,
            },
        }
        kwargs["validate"](payload)
        return payload


def main() -> None:
    segments = {"doc_id": "doc", "segments": [segment(1), segment(2)]}
    classification_client = ClassificationClient()
    classified = classification_agent(classification_client, segments, DEFAULT_CONFIG)
    assert classification_client.rejected == 2
    assert [item["segment"] for item in classified["rows"]] == [1, 2]

    candidate = {"doc_id": "doc", "rows": classified["rows"]}
    reviewed = review_agent(
        ReviewClient(),
        candidate,
        {"doc_id": "doc", "chunks": [{"chunk_id": "c1", "text": "source evidence"}]},
        DEFAULT_CONFIG,
    )
    assert reviewed["status"] == "pass"


if __name__ == "__main__":
    main()
