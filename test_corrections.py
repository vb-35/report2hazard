"""Focused offline correction checks. Run with python test_corrections.py."""

import socket
import urllib.request
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from docx import Document

from multi_hazard_pipeline import human_review, pipeline
from multi_hazard_pipeline.agents.classification_agent import classification_agent
from multi_hazard_pipeline.agents.review_agent import CHECK_NAMES
from multi_hazard_pipeline.agents.segment_agent import SEGMENT_CORRECTION_PROMPT, segment_agent, stabilize_segment_ids
from multi_hazard_pipeline.agents.translation_agent import translation_agent
from multi_hazard_pipeline.config import DEFAULT_CONFIG
from multi_hazard_pipeline.core import read_json, write_json
from multi_hazard_pipeline.errors import PipelineError
from multi_hazard_pipeline.schemas import IMMUTABLE_SEGMENT_FIELDS, validate_segment_chain
from test_regressions import SmokeClient, blocked_network


TEXTS = [
    "Heavy rainfall mobilized sediment into the channel.",
    "That sediment broke the bridge, releasing trapped sediment downstream.",
    "Separately, a rockfall mobilized material on another mountain.",
]


def issue(stage, ids):
    return {"stage": stage, "segment_ids": ids, "code": "fixture_defect",
            "message": "Reassess this fixture against its source.",
            "suggested_action": "Repair the affected step and its consequences."}


def labels(segment_id, corrected=False):
    return {"segment": segment_id,
            "generalized_category": ("Positive Impact on permanent or temporary infrastructure" if corrected
                                     else "Material Mobilization"),
            "interaction_type": "Process-structure" if corrected else "Process-process",
            "sediment_transport_phase": "Transportation" if corrected else "Erosion",
            "classification_rationale": ["T2: " + TEXTS[1] if corrected else "T3: Material mobilization."]}


class CorrectionClient:
    def __init__(self, terminal="pass"):
        self.calls = []
        self.reviews = 0
        self.terminal = terminal
        self.issues = [issue("segmentation", [1]), issue("categorization", [2])]

    def complete_json(self, **kwargs):
        name, data = kwargs["response_schema"]["name"], kwargs["user_payload"]
        self.calls.append((name, deepcopy(data)))
        if name == "causal_segments":
            if "previous_answer" in data and SEGMENT_CORRECTION_PROMPT in kwargs["system_prompt"]:
                assert data["review_issues"] == self.issues
                assert len(data["previous_answer"]["rows"]) == len(data["previous_answer"]["translations"]) == 3
                assert all("classification_rationale" in row for row in data["previous_answer"]["rows"])
                rows = [{key: deepcopy(row[key]) for key in IMMUTABLE_SEGMENT_FIELDS}
                        for row in data["previous_answer"]["rows"]]
                rows[0]["process"] = TEXTS[0]  # Changes segment 2's causal context even though its text stays valid.
            else:
                rows = [{"segment": index, "causal_order": index, "predecessor_segment_ids": [1] if index == 2 else [],
                         "event": "Rainfall" if index < 3 else "Independent rockfall",
                         "process": "Rainfall stored sediment." if index == 1 else chunk["text"],
                         "evidence": [{"chunk_id": chunk["chunk_id"], "quote": chunk["text"]}]}
                        for index, chunk in enumerate(data["chunks"], 1)]
            result = {"segments": rows}
        elif name == "classified_segments":
            if "review_issues" in data:
                assert data["review_issues"] == self.issues  # Both stages' feedback survives segmentation.
                assert [row["segment"] for row in data["segments"]] == [4, 2, 3]
                assert set(data["requested_segment_ids"]) == {4, 2}
                assert data["segments"][1]["predecessor_segment_ids"] == [4]
                assert len(data["previous_answer"]["rows"]) == len(data["source"]["chunks"]) == 3
                result = {"rows": [labels(4), labels(2, corrected=True)]}
                with TestCase().assertRaises(ValueError):
                    kwargs["validate"]({"rows": [labels(4)]})
                with TestCase().assertRaises(ValueError):
                    kwargs["validate"]({"rows": [labels(4), labels(2), labels(999)]})
            else:
                result = {"rows": [labels(row["segment"]) for row in data["segments"]]}
        elif name == "whole_report_evaluation":
            self.reviews += 1
            assert len(data["candidate_report"]["rows"]) == len(data["source_chunks"]) == 3
            if self.reviews == 1:
                result = {"status": "revision_required", "summary": "Two simultaneous fixture defects.",
                          "issues": self.issues,
                          "checks": dict.fromkeys(CHECK_NAMES, True) | {"causal_chain_coherent": False, "categories_valid": False}}
            else:
                rows = data["candidate_report"]["rows"]
                assert rows[0]["segment"] == 4 and rows[1]["predecessor_segment_ids"] == [4]
                assert rows[1]["generalized_category"].startswith("Positive")
                result = {"status": self.terminal, "summary": "Complete corrected report reviewed against all source chunks.",
                          "issues": [] if self.terminal == "pass" else [issue("categorization", [2])],
                          "checks": dict.fromkeys(CHECK_NAMES, True) | {"categories_valid": self.terminal == "pass"}}
        else:
            raise AssertionError(name)
        kwargs["validate"](result)
        return result


def check_correction_and_approval(root):
    inputs = root / "inputs"
    inputs.mkdir()
    doc = Document()
    for text in TEXTS:
        doc.add_paragraph(text)
    doc.save(inputs / "report.docx")
    client = CorrectionClient()
    config = replace(DEFAULT_CONFIG, max_correction_rounds=1)
    manifest = pipeline.run_pipeline(inputs, root / "runs", config, client=client)
    assert manifest["status"] == "awaiting_human_review", manifest["errors"]
    run = Path(manifest["artifact_dir"])
    candidate = read_json(run / "candidate_report.json")
    source = read_json(run / "source.json")
    translated = read_json(run / "translated.json")
    previous = next(data["previous_answer"] for name, data in client.calls if "previous_answer" in data)
    assert candidate["rows"][2] == previous["rows"][2] | {
        "evidence": [{"chunk_id": source["chunks"][2]["chunk_id"], "quote": TEXTS[2],
                      "provenance": candidate["rows"][2]["evidence"][0]["provenance"]}]}
    assert candidate["rows"][1]["segment"] == 2 and candidate["rows"][2]["segment"] == 3
    assert all(row["evidence"][0]["provenance"]["paragraph"] == index for index, row in enumerate(candidate["rows"], 1))
    history = read_json(run / "self_evaluation.json")
    assert history["correction_rounds"][0]["identified_issues"] == client.issues
    assert history["candidate_revision"] == candidate["candidate_revision"] == 2 and client.reviews == 2
    assert not (run / "final_rows.json").exists()
    assert human_review.approve_run(run)["status"] == "approved"

    # A failed complete re-review cannot bypass the run-wide limit or reach approval.
    failing = CorrectionClient(terminal="revision_required")
    failed = pipeline.run_pipeline(inputs, root / "failed-review", config, client=failing)
    assert failed["status"] == "revision_required" and failed["correction_rounds"] == 1 and failing.reviews == 2
    with TestCase().assertRaises(PipelineError):
        human_review.approve_run(failed["artifact_dir"])
    with TestCase().assertRaises(PipelineError):
        human_review.request_correction(failed["artifact_dir"], requested_stage="segmentation", client=failing)

    # A human segmentation request also retains unresolved categorization feedback.
    blocked = pipeline.run_pipeline(inputs, root / "human-request", replace(config, max_correction_rounds=0), client=CorrectionClient())
    blocked_dir = Path(blocked["artifact_dir"])
    blocked["max_correction_rounds"] = 1
    write_json(blocked_dir / "manifest.json", blocked)
    with patch.object(human_review, "correct_until_terminal", side_effect=PipelineError("offline capture")) as correction:
        with TestCase().assertRaises(PipelineError):
            human_review.request_correction(blocked_dir, requested_stage="segmentation", segment_ids=[1], client=object())
        issues = correction.call_args.kwargs["history"]["latest_evaluation"]["issues"]
        assert issues[:2] == client.issues and issues[-1]["segment_ids"] == [1]
    return source, translated, {"rows": previous["rows"]}, client.issues


def check_translation_reuse(source, previous_answer):
    source = deepcopy(source)
    source["chunks"][0]["text"] = "Ein Murgang erreichte den Bach; eine Hangmure blieb am Hang."
    source["chunks"][1]["text"] = "Die Brücke wurde durch Geröll blockiert und zerstört."
    previous = translation_agent(SmokeClient(), source)
    previous["chunks"][1]["translated_text"] = "The bridge was blocked and destroyed by debris."
    rows = deepcopy(previous_answer["rows"])
    for row, chunk in zip(rows, source["chunks"], strict=True):
        row["evidence"] = [{"chunk_id": chunk["chunk_id"], "quote": chunk["text"]}]
    issues = [issue("translation", [1]), issue("categorization", [2])]
    client = SmokeClient()
    revised = translation_agent(client, source, config=DEFAULT_CONFIG, previous_analysis=previous["language_analysis"],
                                previous=previous, previous_answer={"rows": rows}, review_issues=issues)
    assert len(client.calls) == 1 and len(client.calls[0][1]["chunks"]) == 1
    assert client.calls[0][1]["review_issues"] == issues
    assert client.calls[0][1]["previous_answer"]["translations"][1]["translated_text"] == previous["chunks"][1]["translated_text"]
    assert revised["chunks"][1:] == previous["chunks"][1:]
    assert revised["language_analysis"]["chunks"][source["chunks"][1]["chunk_id"]]["translation_applied"]
    for scope in ([], [999]):
        client = SmokeClient()
        translation_agent(client, source, previous=previous, previous_analysis=previous["language_analysis"],
                          previous_answer={"rows": rows}, review_issues=[issue("translation", scope)])
        assert len(client.calls[0][1]["chunks"]) == 2  # Broad or unreliable scope reruns all pending translations.
    uncertain = deepcopy(rows)
    uncertain[0]["evidence"][0]["chunk_id"] = "unknown-source"
    client = SmokeClient()
    translation_agent(client, source, previous=previous, previous_analysis=previous["language_analysis"],
                      previous_answer={"rows": uncertain}, review_issues=[issue("translation", [1])])
    assert len(client.calls[0][1]["chunks"]) == 2  # A known step with unlocatable source must not restrict repair.


def check_chain_operations_and_fallback(source, translated, previous_answer, issues):
    class ChainClient:
        def complete_json(self, **kwargs):
            rows = [{key: deepcopy(row[key]) for key in IMMUTABLE_SEGMENT_FIELDS} for row in previous_answer["rows"]]
            independent = rows[2] | {"causal_order": 1}
            merged = rows[0] | {"segment": 99, "causal_order": 2, "process": "Rainfall and bridge response",
                                "evidence": rows[0]["evidence"] + rows[1]["evidence"]}
            added = rows[1] | {"segment": 100, "causal_order": 3, "predecessor_segment_ids": [99],
                               "process": "Released sediment moved downstream"}
            result = {"segments": [independent, merged, added]}
            kwargs["validate"](result)
            return result

    config = replace(DEFAULT_CONFIG, batch_max_chars=sum(len(chunk["text"]) for chunk in translated["chunks"]))
    chain = segment_agent(ChainClient(), translated, config, previous_answer=previous_answer, review_issues=issues)
    stable = stabilize_segment_ids(chain, {"segments": previous_answer["rows"]})
    validate_segment_chain(stable, source)
    assert stable["segments"][0]["segment"] == 3  # Unchanged ID survives a move.
    assert len(stable["segments"][1]["evidence"]) == 2
    assert stable["segments"][2]["predecessor_segment_ids"] == [stable["segments"][1]["segment"]]
    assert not {1, 2}.intersection(row["segment"] for row in stable["segments"])  # Removed/merged IDs are retired.

    class SizeClient(SmokeClient):
        def complete_json(self, **kwargs):
            if SEGMENT_CORRECTION_PROMPT in kwargs["system_prompt"]:
                raise PipelineError("chat request contains 999999 characters, exceeding max_request_chars=200000")
            return super().complete_json(**kwargs)

    for client, feedback in ((SmokeClient(), [issue("segmentation", [])]),
                             (SmokeClient(), [issue("segmentation", [999])]), (SizeClient(), issues)):
        segment_agent(client, translated, config, previous_answer=previous_answer, review_issues=feedback)
        assert len(client.calls) == 1  # Normal batching is retained; no unconditional halving.
        assert len(client.calls[0][1]["chunks"]) == 3 and client.calls[0][1]["review_issues"] == feedback

    # Categorization can extend beyond the initially flagged IDs when whole-chain context reveals another consequence.
    class CategoryClient:
        def complete_json(self, **kwargs):
            assert kwargs["user_payload"]["requested_segment_ids"] == [1]
            assert len(kwargs["user_payload"]["segments"]) == 3
            result = {"rows": [labels(1), labels(2, corrected=True)]}
            kwargs["validate"](result)
            return result

    classified = classification_agent(CategoryClient(), {"doc_id": source["doc_id"], "segments": previous_answer["rows"]},
                                      config, segment_ids=[1], existing=previous_answer,
                                      source=translated, review_issues=[issue("categorization", [1])])
    assert classified["rows"][1]["generalized_category"].startswith("Positive")
    assert classified["rows"][2] == previous_answer["rows"][2]


def main():
    with (TemporaryDirectory(prefix="hazard-corrections-") as directory,
          patch.object(urllib.request.OpenerDirector, "open", blocked_network),
          patch.object(socket.socket, "connect", blocked_network),
          patch.object(socket.socket, "connect_ex", blocked_network)):
        source, translated, previous, issues = check_correction_and_approval(Path(directory))
        check_translation_reuse(source, previous)
        check_chain_operations_and_fallback(source, translated, previous, issues)
    print("Offline correction checks passed: cross-segment repair, feedback retention, reuse, fallback, whole-report review and approval gates.")


if __name__ == "__main__":
    main()
