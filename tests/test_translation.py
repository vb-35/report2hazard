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


def test_long_source_is_split_without_text_loss_and_translates_offline(tmp_path):
    from multi_hazard_pipeline.agents.source_agent import source_agent
    from multi_hazard_pipeline.agents.segment_agent import batch_chunks
    from multi_hazard_pipeline.core import normalize_text

    path = tmp_path / "long.txt"
    text = normalize_text("Heavy rainfall mobilized sediment. " * 2000)
    path.write_text(text, encoding="utf-8")
    original = source_agent([path], "report")
    chunks = original["chunks"]
    assert len(chunks) > 1
    assert "".join(chunk["text"] for chunk in chunks) == text
    assert len({chunk["chunk_id"] for chunk in chunks}) == len(chunks)
    assert all(chunk["text"] == text[chunk["char_start"]:chunk["char_end"]] for chunk in chunks)

    class EchoClient:
        def complete_json(self, **kwargs):
            batch = kwargs["user_payload"]["chunks"]
            assert sum(len(chunk["text"]) for chunk in batch) <= 5000
            payload = {"translations": [dict(
                chunk_id=chunk["chunk_id"], source_language="English", translated_text=chunk["text"],
            ) for chunk in batch]}
            kwargs["validate"](payload)
            return payload

    translated = translation_agent(EchoClient(), original)
    assert normalize_text(" ".join(chunk["translated_text"] for chunk in translated["chunks"])) == text
    assert [c["char_start"] for c in translated["chunks"]] == [c["char_start"] for c in chunks]
    assert all(sum(len(c["text"]) + len(c["translated_text"]) for c in batch) <= 6000
               for batch in batch_chunks(translated["chunks"], 6000))


def test_batch_rejects_oversized_chunk_instead_of_bypassing_limit():
    from multi_hazard_pipeline.agents.segment_agent import batch_chunks
    from multi_hazard_pipeline.errors import PipelineError

    with pytest.raises(PipelineError, match="exceeding"):
        batch_chunks([{"chunk_id": "long", "text": "x" * 50000}], 5000)
    with pytest.raises(PipelineError, match="exceeding"):
        batch_chunks([{"chunk_id": "bilingual", "text": "x" * 3000, "translated_text": "y" * 3000}], 5000)
