"""Workspace projections, revision boundaries, citations and source allowlist."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from multi_hazard_pipeline.core import read_json, write_json
from multi_hazard_pipeline.pipeline import create_run, save_manifest, set_stage
from multi_hazard_pipeline.web import create_app
from multi_hazard_pipeline.workspace import quote_match, workspace_data
from test_web import QueuedExecutor, evaluation, seeded_run


def test_progressive_artifacts_and_conditional_poll(tmp_path):
    inputs = tmp_path / "input"
    inputs.mkdir()
    (inputs / "report.txt").write_text("Rainfall triggered erosion.")
    manifest = create_run(inputs, tmp_path / "runs", run_id="progressive")
    run = Path(manifest["artifact_dir"])
    client = create_app(run.parent, executor=QueuedExecutor()).test_client()
    get = lambda: client.get("/runs/progressive/workspace").json
    assert not get()["reader"]["source"] and not get()["results"]["rows"]
    write_json(run / "source.json", {"chunks": [{"chunk_id": "chunk-1", "filename": "report.txt", "text": "Rainfall triggered erosion."}]})
    set_stage(run, manifest, "translation")
    assert get()["reader"]["source"]["chunks"][0]["text"] == "Rainfall triggered erosion."
    write_json(run / "translated.json", {"chunks": [{"chunk_id": "chunk-1", "translated_text": "Rainfall triggered erosion."}], "language_analysis": {"decision": "skip_translation"}})
    manifest["stages"]["translation"] = "skipped"
    set_stage(run, manifest, "segmentation")
    assert get()["reader"]["translated"]["language_analysis"]["decision"] == "skip_translation"
    row = {"segment": 7, "causal_order": 1, "event": "Storm", "process": "Erosion", "evidence": [{"chunk_id": "chunk-1", "quote": "erosion"}]}
    write_json(run / "segments.json", {"segments": [row]})
    set_stage(run, manifest, "categorization")
    data = get()
    assert data["results"]["rows"][0]["segment"] == 7
    assert "generalized_category" not in data["results"]["rows"][0]
    assert data["manifest"]["stages"]["segmentation"] == "completed"
    unchanged = client.get("/runs/progressive/workspace", query_string={"since": json.dumps(data["versions"])}).json
    assert "reader" not in unchanged and "results" not in unchanged
    write_json(run / "classified.json", {"rows": [row | {"generalized_category": "Material Mobilization", "classification_rationale": ["Erosion"]}]})
    categorized = client.get("/runs/progressive/workspace", query_string={"since": json.dumps(data["versions"])}).json
    assert "reader" not in categorized
    assert categorized["results"]["rows"][0]["classification_rationale"] == ["Erosion"]


@pytest.mark.parametrize("text,quote,state,expected", [
    ("Heavy\n rainfall\x00mobilized sediment.", "heavy rainfall mobilized", "exact", "Heavy\n rainfall\x00mobilized"),
    ("Straße flooded.", "STRASSE", "exact", "Straße"),
    ("🌧 Rain caused erosion", "rain", "exact", "Rain"),
    ("Flood then Flood", "flood", "repeated", None),
    ("banana", "ana", "repeated", None),
    ("Flood", "missing quotation", "unmatched", None),
    ("Flood", "", "unmatched", None),
])
def test_quote_normalization_preserves_displayed_text(text, quote, state, expected):
    match = quote_match(text, quote)
    assert match["state"] == state
    if expected:
        start, end = match["ranges"][0]
        assert text[start:end] == expected
    else:
        assert not match["ranges"]


def test_companion_citations_and_translation_identity(tmp_path):
    root, manifest = seeded_run(tmp_path)
    run = Path(manifest["artifact_dir"])
    companion = Path(manifest["input_dir"]) / "companion.txt"
    companion.write_text("Flood then Flood. Deposition followed.")
    manifest["inputs"].append({"filename": companion.name, "path": str(companion), "source_type": "txt"})
    save_manifest(run, manifest)
    source = read_json(run / "source.json")
    source["chunks"].append({"chunk_id": "companion-chunk", "document_id": "companion", "filename": companion.name, "paragraph": 2, "table": 1, "row": 2, "cell": 3, "text": companion.read_text()})
    write_json(run / "source.json", source)
    translated = source | {"chunks": [chunk | {"translated_text": "Translated: " + chunk["text"]} for chunk in source["chunks"]]}
    write_json(run / "translated.json", translated)
    candidate = read_json(run / "candidate_report.json")
    candidate["rows"][0]["evidence"].extend([
        {"chunk_id": "companion-chunk", "quote": "Flood"},
        {"chunk_id": "companion-chunk", "quote": "Deposition followed."},
        {"chunk_id": "companion-chunk", "quote": "Unmatched"},
        {"chunk_id": "missing-chunk", "quote": "Unavailable"},
    ])
    write_json(run / "candidate_report.json", candidate)
    data = workspace_data(run)
    evidence = data["results"]["rows"][0]["evidence"]
    assert [item["highlight"]["state"] for item in evidence] == ["exact", "repeated", "exact", "unmatched", "missing"]
    assert evidence[2]["provenance"]["filename"] == "companion.txt"
    assert evidence[2]["provenance"]["cell"] == 3
    assert data["reader"]["documents"][1]["url"].endswith("/source/1")
    assert data["reader"]["translated"]["chunks"][1]["chunk_id"] == evidence[2]["chunk_id"]


def test_correction_never_combines_new_segments_and_old_results(tmp_path):
    root, manifest = seeded_run(tmp_path)
    run = Path(manifest["artifact_dir"])
    client = create_app(root, executor=QueuedExecutor()).test_client()
    assert client.post("/runs/test-run/decision", data={"decision": "request_correction", "requested_stage": "segmentation"}).status_code == 303
    data = workspace_data(run)
    assert data["results"]["previous"]
    assert data["results"]["evaluation"] is None
    row = {"segment": 8, "causal_order": 1, "event": "Replacement", "process": "Erosion", "evidence": []}
    write_json(run / "segments.json", {"segments": [row]})
    data = workspace_data(run)
    assert data["results"]["rows"] == [row] and not data["results"]["previous"]
    assert data["results"]["evaluation"] is None
    write_json(run / "classified.json", {"rows": [row | {"generalized_category": "Material Mobilization"}]})
    assert workspace_data(run)["results"]["kind"] == "classified.json"
    write_json(run / "candidate_report.json", {"candidate_revision": 2, "rows": [row]})
    assert workspace_data(run)["results"]["evaluation"] is None
    write_json(run / "self_evaluation.json", {"candidate_revision": 2, "latest_evaluation": evaluation()})
    assert workspace_data(run)["results"]["evaluation"]["candidate_revision"] == 2
    assert client.get("/runs/test-run/download/final_rows.csv").status_code == 404


def test_retained_translation_and_failed_correction(tmp_path):
    _, manifest = seeded_run(tmp_path)
    run = Path(manifest["artifact_dir"])
    write_json(run / "translated.json", read_json(run / "source.json"))
    set_stage(run, manifest, "translation")
    manifest["status"] = "failed"
    manifest["stages"]["translation"] = "failed"
    save_manifest(run, manifest)
    data = workspace_data(run)
    assert data["reader"]["translation_previous"]
    assert data["results"]["previous"]
    assert data["manifest"]["stages"]["translation"] == "failed"


def test_source_files_allowlist_and_missing_fallback(tmp_path):
    root, manifest = seeded_run(tmp_path)
    client = create_app(root, executor=QueuedExecutor()).test_client()
    assert client.get("/runs/test-run/source/0").status_code == 200
    for identifier in ("1", "00", "-1", "report.txt", "..%2F..%2FREADME.md", "%E2%91%A0"):
        assert client.get(f"/runs/test-run/source/{identifier}").status_code == 404
    assert client.get("/runs/other-run/source/0").status_code == 404
    Path(manifest["inputs"][0]["path"]).unlink()
    assert client.get("/runs/test-run/source/0").status_code == 404
    data = client.get("/runs/test-run/workspace").json
    assert not data["reader"]["documents"][0]["available"]
    assert data["reader"]["source"]["chunks"]


def test_evaluation_revision_mismatch_blocks_approval(tmp_path):
    root, manifest = seeded_run(tmp_path)
    run = Path(manifest["artifact_dir"])
    history = read_json(run / "self_evaluation.json")
    history["candidate_revision"] = 0
    write_json(run / "self_evaluation.json", history)
    assert workspace_data(run)["results"]["evaluation"] is None
    client = create_app(root, executor=QueuedExecutor()).test_client()
    assert client.post("/runs/test-run/decision", data={"decision": "approve"}).status_code == 409
    assert not (run / "final_rows.json").exists()


def test_document_content_is_escaped_and_original_pdf_is_inline(tmp_path):
    root, manifest = seeded_run(tmp_path)
    run = Path(manifest["artifact_dir"])
    source = read_json(run / "source.json")
    source["chunks"][0]["text"] = "</script><img src=x onerror=alert(1)>"
    write_json(run / "source.json", source)
    client = create_app(root, executor=QueuedExecutor()).test_client()
    html = client.get("/runs/test-run").data
    assert b"<img src=x" not in html
    pdf = Path(manifest["input_dir"]) / "original.pdf"
    pdf.write_bytes(b"%PDF-1.4\n%%EOF")
    manifest["inputs"].append({"filename": pdf.name, "path": str(pdf), "source_type": "pdf"})
    save_manifest(run, manifest)
    response = client.get("/runs/test-run/source/1")
    assert response.status_code == 200
    assert response.headers["Content-Type"] == "application/pdf"
    assert response.headers["Content-Disposition"].startswith("inline")


def test_legacy_inflight_results_and_invalid_poll_parameters(tmp_path):
    root, manifest = seeded_run(tmp_path)
    run = Path(manifest["artifact_dir"])
    manifest["status"] = "running"
    manifest["current_stage"] = "segmentation"
    save_manifest(run, manifest)
    data = workspace_data(run)
    assert data["results"]["previous"] and data["results"]["evaluation"] is None
    client = create_app(root, executor=QueuedExecutor()).test_client()
    for since in ("[]", "bad json"):
        assert client.get("/runs/test-run/workspace", query_string={"since": since}).status_code == 400
    assert client.get("/runs/test-run/source/" + "9" * 100).status_code == 404


def test_source_replacement_refreshes_citation_matches(tmp_path):
    _, manifest = seeded_run(tmp_path)
    run = Path(manifest["artifact_dir"])
    data = workspace_data(run)
    source = read_json(run / "source.json")
    source["chunks"][0]["text"] = "Replacement source text."
    write_json(run / "source.json", source)
    updated = workspace_data(run, data["versions"])
    assert updated["results"]["rows"][0]["evidence"][0]["highlight"]["state"] == "unmatched"


def test_saved_text_without_original_input_metadata(tmp_path):
    root, manifest = seeded_run(tmp_path)
    run = Path(manifest["artifact_dir"])
    manifest["inputs"] = []
    save_manifest(run, manifest)
    data = workspace_data(run)
    assert data["reader"]["documents"][0]["filename"] == "report.txt"
    assert not data["reader"]["documents"][0]["available"]
    client = create_app(root, executor=QueuedExecutor()).test_client()
    assert client.get("/runs/test-run/source/saved-0").status_code == 404


def test_editor_preserves_unsaved_input_across_updates():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is needed for the dependency-free JavaScript regression check")
    subprocess.run([node, str(Path(__file__).with_name("workspace_editor_check.js"))], check=True)


@pytest.mark.parametrize("decision", ["approve", "reject", "request_correction"])
def test_prepopulated_segment_does_not_require_an_optional_comment(tmp_path, decision):
    root, _ = seeded_run(tmp_path)
    client = create_app(root, executor=QueuedExecutor()).test_client()
    response = client.post("/runs/test-run/decision", data={
        "decision": decision, "comment_segment": "1", "segment_ids": "1",
        "requested_stage": "categorization", "issue_type": "", "segment_comment": "",
    })
    assert response.status_code == 303
