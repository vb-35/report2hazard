from __future__ import annotations

from copy import deepcopy

import pytest

from multi_hazard_pipeline.agents.segment_agent import segment_agent
from multi_hazard_pipeline.agents.translation_agent import translation_agent
from multi_hazard_pipeline.config import DEFAULT_CONFIG


def source(text: str) -> dict:
    return {
        "doc_id": "report",
        "status": "pass",
        "chunks": [
            {
                "chunk_id": "c1",
                "document_id": "document-1",
                "filename": "report.txt",
                "source_type": "txt",
                "paragraph": 4,
                "text": text,
            }
        ],
    }


class PayloadClient:
    def __init__(self, payload: dict):
        self.payload = payload
        self.calls = []

    def complete_json(self, **kwargs):
        self.calls.append(kwargs)
        payload = deepcopy(self.payload)
        kwargs["validate"](payload)
        return payload


def test_english_translation_is_normalized_passthrough() -> None:
    original = source("Heavy rainfall mobilized sediment into the channel.")
    client = PayloadClient(
        {
            "translations": [
                {
                    "chunk_id": "c1",
                    "source_language": "English",
                    "translated_text": "Heavy rainfall mobilized sediment into the channel.",
                }
            ]
        }
    )
    translated = translation_agent(client, original)
    assert translated["chunks"][0]["text"] == original["chunks"][0]["text"]
    assert translated["chunks"][0]["paragraph"] == 4
    assert translated["chunks"][0]["translated_text"] == original["chunks"][0]["text"]
    assert len(client.calls[0]["user_payload"]["terminology_mappings"]) == 151


def test_german_translation_uses_glossary_as_context() -> None:
    original = source("Ein Murgang erreichte den Bach; eine Hangmure blieb am Hang.")
    client = PayloadClient(
        {
            "translations": [
                {
                    "chunk_id": "c1",
                    "source_language": "German",
                    "translated_text": (
                        "A debris flow reached the torrent; a slope debris flow remained on the slope."
                    ),
                }
            ]
        }
    )
    translated = translation_agent(client, original)
    glossary = client.calls[0]["user_payload"]["terminology_mappings"]
    assert next(row for row in glossary if row["German"] == "Murgang")["English"] == "debris flow"
    assert next(row for row in glossary if row["German"] == "Hangmure")["English"] == "Slope debris flow"
    assert "slope debris flow" in translated["chunks"][0]["translated_text"]


def test_large_translation_is_batched_and_reassembled_in_order() -> None:
    original = source("A" * 3000)
    original["chunks"].extend(
        {**original["chunks"][0], "chunk_id": f"c{index}", "text": letter * 3000}
        for index, letter in ((2, "B"), (3, "C"))
    )

    class BatchClient:
        def __init__(self):
            self.calls = []

        def complete_json(self, **kwargs):
            self.calls.append(kwargs)
            payload = {
                "translations": [
                    {"chunk_id": chunk["chunk_id"], "source_language": "English", "translated_text": chunk["text"]}
                    for chunk in kwargs["user_payload"]["chunks"]
                ]
            }
            kwargs["validate"](payload)
            return payload

    client = BatchClient()
    translated = translation_agent(client, original)
    assert len(client.calls) == 3
    assert [call["user_payload"]["chunks"][0]["chunk_id"] for call in client.calls] == ["c1", "c2", "c3"]
    assert [chunk["translated_text"][0] for chunk in translated["chunks"]] == ["A", "B", "C"]


def test_segmentation_interprets_translation_but_quotes_original_language() -> None:
    translated = source("Starker Regen mobilisierte Sediment in das Gerinne.")
    translated["chunks"][0].update(
        {
            "source_language": "German",
            "translated_text": "Heavy rainfall mobilized sediment into the channel.",
        }
    )
    client = PayloadClient(
        {
            "segments": [
                {
                    "segment": 1,
                    "causal_order": 1,
                    "predecessor_segment_ids": [],
                    "event": "Catchment rainfall",
                    "process": "Heavy rainfall mobilized sediment into the channel",
                    "evidence": [
                        {
                            "chunk_id": "c1",
                            "quote": "Starker Regen mobilisierte Sediment in das Gerinne.",
                        }
                    ],
                }
            ]
        }
    )
    segments = segment_agent(client, translated, DEFAULT_CONFIG)
    assert client.calls[0]["user_payload"]["chunks"][0]["translated_text"].startswith("Heavy rainfall")
    assert segments["segments"][0]["evidence"][0]["quote"].startswith("Starker Regen")


@pytest.mark.parametrize(
    "translations,message",
    [
        ([], "missing chunk IDs"),
        (
            [
                {"chunk_id": "c1", "source_language": "German", "translated_text": "Debris flow"},
                {"chunk_id": "c1", "source_language": "German", "translated_text": "Debris flow"},
            ],
            "duplicate chunk IDs",
        ),
    ],
)
def test_missing_or_duplicate_translation_results_are_rejected(translations, message) -> None:
    client = PayloadClient({"translations": translations})
    with pytest.raises(ValueError, match=message):
        translation_agent(client, source("Ein Murgang trat auf."))
