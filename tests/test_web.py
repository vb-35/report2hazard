from __future__ import annotations

from pathlib import Path

import pytest

from multi_hazard_pipeline.core import read_json, write_json
from multi_hazard_pipeline.pipeline import create_human_review, create_run, save_manifest
from multi_hazard_pipeline.web import create_app


class QueuedExecutor:
    def __init__(self) -> None:
        self.jobs = []

    def submit(self, function):
        self.jobs.append(function)


class ImmediateExecutor:
    def submit(self, function):
        function()


def evaluation(status: str = "pass") -> dict:
    return {
        "status": status,
        "summary": "Coherent" if status == "pass" else "Needs work",
        "issues": [] if status == "pass" else [
            {
                "stage": "categorization",
                "segment_ids": [1],
                "code": "bad_category",
                "message": "Wrong category",
                "suggested_action": "Reclassify",
            }
        ],
        "checks": {
            "causal_chain_coherent": True,
            "evidence_verified": True,
            "segments_complete": True,
            "segments_unique": True,
            "categories_valid": status == "pass",
        },
    }


def seeded_run(tmp_path: Path, status: str = "awaiting_human_review") -> tuple[Path, dict]:
    input_dir = tmp_path / "input"
    input_dir.mkdir(parents=True, exist_ok=True)
    (input_dir / "report.txt").write_text(
        "Heavy rainfall mobilized sediment into the channel.", encoding="utf-8"
    )
    root = tmp_path / "runs"
    manifest = create_run(input_dir, root, run_id="test-run")
    run_dir = Path(manifest["artifact_dir"])
    chunk_id = "report-document-001-chunk-0001"
    source = {
        "doc_id": "report",
        "chunks": [{
            "chunk_id": chunk_id,
            "doc_id": "report",
            "filename": "report.txt",
            "source_type": "txt",
            "text": "Heavy rainfall mobilized sediment into the channel.",
        }],
    }
    segment = {
        "segment": 1,
        "causal_order": 1,
        "predecessor_segment_ids": [],
        "event": "Catchment event",
        "process": "Rainfall mobilized sediment",
        "evidence": [{"chunk_id": chunk_id, "quote": source["chunks"][0]["text"]}],
    }
    row = segment | {
        "generalized_category": "Material Mobilization",
        "interaction_type": "Process-process",
        "sediment_transport_phase": "Erosion",
        "classification_rationale": ["Mobilization"],
    }
    candidate_row = row | {
        "evidence": [{
            "chunk_id": chunk_id,
            "quote": source["chunks"][0]["text"],
            "provenance": {"filename": "report.txt", "source_type": "txt"},
        }]
    }
    candidate = {
        "run_id": "test-run",
        "doc_id": "report",
        "status": "candidate",
        "candidate_revision": 1,
        "rows": [candidate_row],
    }
    latest = evaluation("pass" if status in {"awaiting_human_review", "approved", "rejected"} else "revision_required")
    history = {
        "status": latest["status"],
        "latest_evaluation": latest,
        "evaluations": [{"round": 0, "evaluation": latest, "timestamp": "2026-01-01T00:00:00Z"}],
        "correction_rounds": [],
    }
    write_json(run_dir / "source.json", source)
    write_json(run_dir / "segments.json", {"doc_id": "report", "segments": [segment]})
    write_json(run_dir / "classified.json", {"doc_id": "report", "rows": [row]})
    write_json(run_dir / "candidate_report.json", candidate)
    write_json(run_dir / "self_evaluation.json", history)
    if status in {"awaiting_human_review", "approved", "rejected"}:
        write_json(run_dir / "human_review.json", create_human_review(manifest, candidate))
    manifest["status"] = status
    manifest["current_stage"] = "human_review" if status == "awaiting_human_review" else "self_evaluation"
    manifest["progress"] = 90 if status == "awaiting_human_review" else 80
    save_manifest(run_dir, manifest)
    return root, manifest


def test_index_detail_and_status_routes_render(tmp_path: Path) -> None:
    root, manifest = seeded_run(tmp_path)
    client = create_app(root, executor=QueuedExecutor()).test_client()
    index = client.get("/")
    detail = client.get(f"/runs/{manifest['run_id']}")
    status = client.get(f"/runs/{manifest['run_id']}/status")
    assert index.status_code == 200 and b"test-run" in index.data
    assert detail.status_code == 200
    assert b"Awaiting human review" in detail.data
    assert b"Rainfall mobilized sediment" in detail.data
    assert b"report.txt" in detail.data
    assert status.json["status"] == "awaiting_human_review"


def test_create_run_writes_manifest_before_background_submission(tmp_path: Path) -> None:
    input_dir = tmp_path / "new-report"
    input_dir.mkdir()
    (input_dir / "report.txt").write_text("Flood deposited sediment.", encoding="utf-8")
    executor = QueuedExecutor()
    root = tmp_path / "runs"
    client = create_app(root, executor=executor).test_client()
    response = client.post("/runs", data={"input_dir": str(input_dir)})
    assert response.status_code == 303
    run_id = response.headers["Location"].rstrip("/").split("/")[-1]
    assert (root / run_id / "manifest.json").is_file()
    assert read_json(root / run_id / "manifest.json")["current_stage"] == "queued"
    assert len(executor.jobs) == 1


def test_upload_create_accepts_supported_files(tmp_path: Path) -> None:
    from io import BytesIO

    executor = QueuedExecutor()
    client = create_app(tmp_path / "runs", executor=executor).test_client()
    response = client.post(
        "/runs",
        data={"files": [(BytesIO(b"Flood deposited sediment."), "report.txt")]},
        content_type="multipart/form-data",
    )
    assert response.status_code == 303
    assert len(executor.jobs) == 1


def test_uploaded_collection_creates_one_ordinary_run_per_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from io import BytesIO

    split_dir = tmp_path / "prepared"
    split_dir.mkdir()
    event_pdfs = [split_dir / "01-first.pdf", split_dir / "02-second.pdf"]
    for path in event_pdfs:
        path.write_bytes(b"%PDF-1.4\n%%EOF")
    monkeypatch.setattr(
        "multi_hazard_pipeline.web.ChatClient.from_config", lambda config: object()
    )
    monkeypatch.setattr(
        "multi_hazard_pipeline.web.split_event_reports",
        lambda *args, **kwargs: event_pdfs,
    )
    executor = QueuedExecutor()
    root = tmp_path / "runs"
    client = create_app(root, executor=executor).test_client()
    response = client.post(
        "/runs",
        data={"files": [(BytesIO(b"%PDF-1.4\n%%EOF"), "collection.pdf")]},
        content_type="multipart/form-data",
    )
    manifests = list(root.glob("*/manifest.json"))
    assert response.status_code == 303
    assert len(manifests) == 2
    assert sorted(path.parent.name.split("__")[0] for path in manifests) == ["01-first", "02-second"]
    assert len(executor.jobs) == 2
    assert sorted(read_json(path)["inputs"][0]["filename"] for path in manifests) == [
        "01-first.pdf",
        "02-second.pdf",
    ]


def test_candidate_download_and_approved_download_gate(tmp_path: Path) -> None:
    root, manifest = seeded_run(tmp_path)
    client = create_app(root, executor=QueuedExecutor()).test_client()
    candidate = client.get(f"/runs/{manifest['run_id']}/download/candidate_report.json")
    final = client.get(f"/runs/{manifest['run_id']}/download/final_rows.json")
    unknown = client.get(f"/runs/{manifest['run_id']}/download/source.json")
    assert candidate.status_code == 200
    assert final.status_code == 404
    assert unknown.status_code == 404


def test_ui_cannot_approve_candidate_without_passed_self_evaluation(tmp_path: Path) -> None:
    root, manifest = seeded_run(tmp_path, "revision_required")
    client = create_app(root, executor=QueuedExecutor()).test_client()
    response = client.post(
        f"/runs/{manifest['run_id']}/decision", data={"decision": "approve"}
    )
    assert response.status_code == 409
    assert not (Path(manifest["artifact_dir"]) / "final_rows.json").exists()


def test_approval_exposes_authoritative_downloads(tmp_path: Path) -> None:
    root, manifest = seeded_run(tmp_path)
    client = create_app(root, executor=QueuedExecutor()).test_client()
    approved = client.post(
        f"/runs/{manifest['run_id']}/decision",
        data={"decision": "approve", "global_comment": "Looks correct"},
    )
    assert approved.status_code == 303
    assert client.get(f"/runs/{manifest['run_id']}/download/final_rows.json").status_code == 200
    assert client.get(f"/runs/{manifest['run_id']}/download/final_rows.csv").status_code == 200


def test_llm_human_actions_are_queued(tmp_path: Path) -> None:
    root, manifest = seeded_run(tmp_path)
    executor = QueuedExecutor()
    client = create_app(root, executor=executor).test_client()
    correction = client.post(
        f"/runs/{manifest['run_id']}/decision",
        data={
            "decision": "request_correction",
            "requested_stage": "categorization",
            "segment_ids": "1",
            "global_comment": "Reclassify this segment",
        },
    )
    assert correction.status_code == 303
    assert read_json(Path(manifest["artifact_dir"]) / "manifest.json")["status"] == "running"
    assert len(executor.jobs) == 1

    edit_root, edit_manifest = seeded_run(tmp_path / "edit")
    edit_executor = QueuedExecutor()
    edit_client = create_app(edit_root, executor=edit_executor).test_client()
    edit = client.post(
        f"/runs/{manifest['run_id']}/edit",
        data={"segment": "1", "field": "interaction_type", "new_value": "Process-topography"},
    )
    assert edit.status_code == 409
    edit = edit_client.post(
        f"/runs/{edit_manifest['run_id']}/edit",
        data={"segment": "1", "field": "interaction_type", "new_value": "Process-topography"},
    )
    assert edit.status_code == 303
    assert read_json(Path(edit_manifest["artifact_dir"]) / "manifest.json")["status"] == "running"
    assert len(edit_executor.jobs) == 1


def test_approval_is_blocked_as_soon_as_reevaluation_is_queued(tmp_path: Path) -> None:
    root, manifest = seeded_run(tmp_path)
    executor = QueuedExecutor()
    client = create_app(root, executor=executor).test_client()
    queued = client.post(
        f"/runs/{manifest['run_id']}/edit",
        data={"segment": "1", "field": "interaction_type", "new_value": "Process-topography"},
    )
    approval = client.post(
        f"/runs/{manifest['run_id']}/decision", data={"decision": "approve"}
    )
    assert queued.status_code == 303
    assert approval.status_code == 409
    assert not (Path(manifest["artifact_dir"]) / "final_rows.json").exists()


def test_background_failure_is_recorded_in_manifest(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    input_dir = tmp_path / "new-report"
    input_dir.mkdir()
    (input_dir / "report.txt").write_text("Flood deposited sediment.", encoding="utf-8")

    def fail(*args, **kwargs):
        raise RuntimeError("worker unavailable")

    monkeypatch.setattr("multi_hazard_pipeline.web.execute_run", fail)
    root = tmp_path / "runs"
    client = create_app(root, executor=ImmediateExecutor()).test_client()
    response = client.post("/runs", data={"input_dir": str(input_dir)})
    run_id = response.headers["Location"].rstrip("/").split("/")[-1]
    manifest = read_json(root / run_id / "manifest.json")
    assert manifest["status"] == "failed"
    assert manifest["errors"][-1]["stage"] == "pipeline"
    assert "worker unavailable" in manifest["errors"][-1]["message"]
