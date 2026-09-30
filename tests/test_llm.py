import json
import pytest
from dataclasses import replace
from pathlib import Path

from multi_hazard_pipeline.config import DEFAULT_CONFIG, LLMConfig
from multi_hazard_pipeline.errors import PipelineError
from multi_hazard_pipeline.core import parse_json_object
from multi_hazard_pipeline.llm import ChatClient
from multi_hazard_pipeline.schemas import validate_review_payload


def test_api_address_must_be_supplied_locally(monkeypatch):
    monkeypatch.delenv("TW_LLM_API_BASE_URL", raising=False)
    assert LLMConfig().api_base_url == ""
    with pytest.raises(PipelineError, match="TW_LLM_API_BASE_URL"):
        ChatClient.from_config(LLMConfig())
    monkeypatch.setenv("TW_LLM_API_BASE_URL", " https://example.invalid/v1 ")
    monkeypatch.setenv("TW_LLM_API_KEY", "test-only")
    assert ChatClient.from_config(LLMConfig()).config.api_base_url == "https://example.invalid/v1"


def test_validation_retry_tells_model_how_to_correct_response(monkeypatch, tmp_path: Path) -> None:
    timing_path = tmp_path / "timings.jsonl"
    client = ChatClient("test-key", replace(DEFAULT_CONFIG.llm, retries=2), timing_path)
    prompts = []
    responses = [
        {"status": "pass", "summary": "Fine", "issues": [], "checks": {}},
        {
            "status": "pass",
            "summary": "Fine",
            "issues": [],
            "checks": {
                "causal_chain_coherent": True,
                "evidence_verified": True,
                "segments_complete": True,
                "segments_unique": True,
                "categories_valid": True,
            },
        },
    ]

    def fake_post(**kwargs):
        prompts.append(kwargs["system_prompt"])
        return responses.pop(0)

    monkeypatch.setattr(client, "_post", fake_post)
    result = client.complete_json(
        system_prompt="Evaluate report.",
        user_payload={},
        validate=validate_review_payload,
    )

    assert result["status"] == "pass"
    assert "checks are incomplete" in prompts[1]
    timings = [json.loads(line) for line in timing_path.read_text(encoding="utf-8").splitlines()]
    assert [(item["attempt"], item["outcome"]) for item in timings] == [
        (1, "validation_error"), (2, "pass"),
    ]
    assert all(item["input_chars"] >= 2 and item["elapsed_seconds"] >= 0 for item in timings)
    assert all("test-key" not in line and "Evaluate report" not in line for line in timing_path.read_text(encoding="utf-8").splitlines())


def test_json_parser_accepts_valid_object_with_trailing_model_text() -> None:
    assert parse_json_object('{"segments":[]} Extra explanation') == {"segments": []}


def test_json_parser_accepts_unescaped_control_character_in_model_string() -> None:
    assert parse_json_object('{"process":"Rainfall\ncaused erosion"}') == {"process": "Rainfall\ncaused erosion"}
