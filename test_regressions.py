"""Small offline research smoke check. Run with python test_regressions.py."""

import csv
import socket
import urllib.request
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from docx import Document

from multi_hazard_pipeline import human_review, language, pipeline
from multi_hazard_pipeline.agents.review_agent import CHECK_NAMES
from multi_hazard_pipeline.agents.source_agent import source_agent, split_source_chunks
from multi_hazard_pipeline.agents.translation_agent import translation_agent
from multi_hazard_pipeline.config import DEFAULT_CONFIG
from multi_hazard_pipeline.core import read_json, write_json
from multi_hazard_pipeline.errors import PipelineError
from multi_hazard_pipeline.schemas import validate_segment_chain


class SmokeClient:
    """Fixed model responses; exercise the real stages without an API key."""

    def __init__(self):
        self.calls = []

    def complete_json(self, **kwargs):
        name = kwargs["response_schema"]["name"]
        data = kwargs["user_payload"]
        self.calls.append((name, data))
        if name == "translated_chunks":
            payload = {"translations": [
                {"chunk_id": chunk["chunk_id"], "source_language": chunk["source_language"],
                 "translated_text": "A debris flow reached the torrent."}
                for chunk in data["chunks"]
            ]}
        elif name == "causal_segments":
            payload = {"segments": [
                {"segment": number, "causal_order": number,
                 "predecessor_segment_ids": [] if number == 1 else [number - 1],
                 "event": "Catchment storm", "process": chunk["translated_text"],
                 "evidence": [{"chunk_id": chunk["chunk_id"], "quote": chunk["text"]}]}
                for number, chunk in enumerate(data["chunks"], start=1)
            ]}
        elif name == "classified_segments":
            payload = {"rows": [
                {"segment": item["segment"],
                 "generalized_category": "Material Mobilization" if index == 0 else "Negative Impact on permanent or temporary infrastructure",
                 "interaction_type": "Process-process" if index == 0 else "Process-structure",
                 "sediment_transport_phase": "Erosion" if index == 0 else "Dysconnectivity",
                 "classification_rationale": ["Rainfall recruits sediment." if index == 0 else "Debris blocks the bridge."]}
                for index, item in enumerate(data["segments"])
            ]}
            with TestCase().assertRaises(ValueError):
                kwargs["validate"]({"rows": payload["rows"][:1]})
            with TestCase().assertRaises(ValueError):
                kwargs["validate"]({"rows": [payload["rows"][0]] * 2})
        elif name == "whole_report_evaluation":
            assert len(data["candidate_report"]["rows"]) == len(data["source_chunks"])
            payload = {"status": "pass", "summary": "Synthetic chain is coherent.",
                       "issues": [], "checks": dict.fromkeys(CHECK_NAMES, True)}
        else:
            raise AssertionError(f"Unexpected model call: {name}")
        kwargs["validate"](payload)
        return payload


def check_extraction_and_translation(root):
    inputs = root / "input"
    inputs.mkdir()
    path = inputs / "report.docx"
    document = Document()
    document.add_paragraph("Heavy rainfall mobilized sediment into the channel.")
    document.add_table(rows=1, cols=1).cell(0, 0).text = "The bridge became blocked by debris."
    document.save(path)
    source = source_agent([path], "report")
    assert [chunk["text"] for chunk in source["chunks"]] == [
        "Heavy rainfall mobilized sediment into the channel.", "The bridge became blocked by debris.",
    ]
    assert source["chunks"][0]["paragraph"] == 1
    assert source["chunks"][1]["table"] == source["chunks"][1]["cell"] == 1
    split = split_source_chunks(source["chunks"], 20)
    assert "".join(chunk["text"] for chunk in split) == "".join(chunk["text"] for chunk in source["chunks"])
    assert len({chunk["chunk_id"] for chunk in split}) == len(split)

    client = SmokeClient()
    translated = translation_agent(client, source)
    assert not client.calls
    assert [chunk["translated_text"] for chunk in translated["chunks"]] == [chunk["text"] for chunk in source["chunks"]]

    german = deepcopy(source)
    german["chunks"] = [german["chunks"][0]]
    german["chunks"][0]["text"] = "Ein Murgang erreichte den Bach; eine Hangmure blieb am Hang."
    translated = translation_agent(client, german)
    assert translated["chunks"][0]["text"] == german["chunks"][0]["text"]
    assert translated["chunks"][0]["paragraph"] == 1
    assert translated["chunks"][0]["translated_text"] == "A debris flow reached the torrent."
    glossary = client.calls[0][1]["terminology_mappings"]
    assert all(set(row) == {"English", "German"} for row in glossary)
    assert next(row for row in glossary if row["German"] == "Murgang")["English"] == "debris flow"
    assert language.majority_decision(100, 76, 0, .75)["decision"] == "skip_translation"
    assert language.majority_decision(100, 75, 0, .75)["decision"] == "translate"
    assert language.majority_decision(100, 70, 20, .75)["decision"] == "unresolved"
    return inputs


def check_pipeline_and_exports(root, inputs):
    client = SmokeClient()
    manifest = pipeline.run_pipeline(inputs, root / "runs", client=client)
    assert manifest["status"] == "awaiting_human_review", manifest.get("errors")
    run = Path(manifest["artifact_dir"])
    candidate = read_json(run / "candidate_report.json")
    source = read_json(run / "source.json")
    assert [name for name, _ in client.calls] == [
        "causal_segments", "classified_segments", "whole_report_evaluation",
    ]
    assert [row["process"] for row in candidate["rows"]] == [chunk["text"] for chunk in source["chunks"]]
    assert candidate["rows"][1]["evidence"][0]["provenance"]["table"] == 1
    assert not (run / "final_rows.json").exists() and not (run / "final_rows.csv").exists()
    invalid = {"segments": deepcopy(candidate["rows"])}
    invalid["segments"][0]["evidence"][0]["quote"] = "Invented source evidence"
    with TestCase().assertRaises(ValueError):
        validate_segment_chain(invalid, source)

    edited = human_review.apply_candidate_edits(
        run, [{"segment": 1, "field": "interaction_type", "new_value": "Process-topography"}],
        client=client,
    )
    assert edited["status"] == "awaiting_human_review"
    candidate = read_json(run / "candidate_report.json")
    assert candidate["candidate_revision"] == 2
    assert read_json(run / "self_evaluation.json")["candidate_revision"] == 2
    history = read_json(run / "self_evaluation.json")
    write_json(run / "self_evaluation.json", history | {"candidate_revision": 1})
    with TestCase().assertRaises(PipelineError):
        human_review.approve_run(run)
    assert not (run / "final_rows.json").exists() and not (run / "final_rows.csv").exists()
    write_json(run / "self_evaluation.json", history)
    approved = human_review.approve_run(run)
    assert approved["status"] == read_json(run / "final_rows.json")["status"] == "approved"
    with (run / "final_rows.csv").open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        assert reader.fieldnames == list(DEFAULT_CONFIG.export_columns)
        rows = list(reader)
    assert len(rows) == 2
    assert rows[0]["process"] == source["chunks"][0]["text"]
    assert rows[0]["interaction_type"] == "Process-topography"

    failed = pipeline.run_pipeline(inputs, root / "failed", client=object())
    assert failed["status"] == "failed"
    assert failed["errors"][0]["stage"] == "segmentation"
    assert not (Path(failed["artifact_dir"]) / "final_rows.json").exists()

    manifest = pipeline.run_pipeline(inputs, root / "export_failure", client=SmokeClient())
    run = Path(manifest["artifact_dir"])
    with patch.object(human_review, "write_csv", side_effect=OSError("disk full")):
        with TestCase().assertRaises(OSError):
            human_review.approve_run(run)
    assert read_json(run / "manifest.json")["status"] == "failed"
    assert not (run / "final_rows.json").exists() and not (run / "final_rows.csv").exists()


def blocked_network(*args, **kwargs):
    raise AssertionError("The smoke check must not contact a model service or network")


def main():
    with (
        TemporaryDirectory(prefix="hazard-smoke-") as directory,
        patch.object(urllib.request.OpenerDirector, "open", blocked_network),
        patch.object(socket.socket, "connect", blocked_network),
        patch.object(socket.socket, "connect_ex", blocked_network),
    ):
        root = Path(directory)
        inputs = check_extraction_and_translation(root)
        check_pipeline_and_exports(root, inputs)
    print("Research smoke check passed (offline; extraction, translation, evidence, classification, review and export).")


if __name__ == "__main__":
    main()
