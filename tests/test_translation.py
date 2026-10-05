from __future__ import annotations

from copy import deepcopy
from importlib import import_module

import pytest

from multi_hazard_pipeline.agents.segment_agent import segment_agent
from multi_hazard_pipeline.agents.translation_agent import translation_agent
from multi_hazard_pipeline.config import DEFAULT_CONFIG
from multi_hazard_pipeline.core import normalize_text
from multi_hazard_pipeline import language


PASSAGES = {
    "English": "Heavy rainfall mobilized sediment into the channel. The bridge was blocked by debris and the river overflowed, causing upstream flooding and erosion of the channel banks.",
    "German": "Starke Niederschläge mobilisierten große Mengen Sediment im Einzugsgebiet. Der Murgang erreichte die Brücke und blockierte das Gerinne, wodurch es zu Überschwemmungen kam.",
    "French": "De fortes précipitations ont mobilisé de grandes quantités de sédiments dans le bassin versant. La coulée de débris a bloqué le pont et provoqué des inondations en amont.",
    "Italian": "Le forti precipitazioni hanno mobilizzato grandi quantità di sedimenti nel bacino. La colata detritica ha bloccato il ponte e provocato inondazioni a monte del torrente.",
}


class NoCallsClient:
    def complete_json(self, **kwargs):
        raise AssertionError("No translation call should be made")


class TranslationClient:
    def __init__(self):
        self.calls = []

    def complete_json(self, **kwargs):
        self.calls.append(kwargs)
        payload = {"translations": [
            {"chunk_id": chunk["chunk_id"],
             "source_language": "German" if chunk["source_language"] == "Unknown" else chunk["source_language"],
             "translated_text": "Faithful English translation."}
            for chunk in kwargs["user_payload"]["chunks"]
        ]}
        kwargs["validate"](payload)
        return payload


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
    assert client.calls == []
    assert translated["language_analysis"]["decision"] == "skip_translation"


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
    assert all(set(row) == {"English", "German"} for row in glossary)
    assert next(row for row in glossary if row["German"] == "Murgang")["English"] == "debris flow"
    assert next(row for row in glossary if row["German"] == "Hangmure")["English"] == "Slope debris flow"
    assert "slope debris flow" in translated["chunks"][0]["translated_text"]


def test_large_translation_is_batched_and_reassembled_in_order() -> None:
    original = source("Starker Regen mobilisierte Sediment in das Gerinne. " * 60)
    original["chunks"].extend(
        {**original["chunks"][0], "chunk_id": f"c{index}", "text": original["chunks"][0]["text"]}
        for index in (2, 3)
    )

    class BatchClient:
        def __init__(self):
            self.calls = []

        def complete_json(self, **kwargs):
            self.calls.append(kwargs)
            payload = {
                "translations": [
                    {"chunk_id": chunk["chunk_id"], "source_language": "German", "translated_text": "Heavy rainfall mobilized sediment into the channel."}
                    for chunk in kwargs["user_payload"]["chunks"]
                ]
            }
            kwargs["validate"](payload)
            return payload

    client = BatchClient()
    translated = translation_agent(client, original)
    assert len(client.calls) == 3
    assert [call["user_payload"]["chunks"][0]["chunk_id"] for call in client.calls] == ["c1", "c2", "c3"]
    assert [chunk["chunk_id"] for chunk in translated["chunks"]] == ["c1", "c2", "c3"]


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
        translation_agent(client, source("Ein Murgang erreichte den Bach; eine Hangmure blieb am Hang."))


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


def test_offline_language_detection_and_mixed_split_report():
    from multi_hazard_pipeline.agents.source_agent import split_source_chunks

    for name, passage in PASSAGES.items():
        analysis = language.analyze_language(source(passage))
        assert analysis["counts"][name] == language.alphabetic_count(passage)
        assert analysis["decision"] == ("skip_translation" if name == "English" else "translate")
    original = source(" ".join(PASSAGES.values()))
    analysis = language.analyze_language(original)
    assert analysis["chunks"]["c1"]["source_language"] == "Mixed"
    assert analysis["decision"] == "translate"
    split = original | {"chunks": split_source_chunks(original["chunks"], 83)}
    assert language.analyze_language(split)["counts"] == analysis["counts"]


def test_english_majority_threshold_and_uncertainty():
    assert language.majority_decision(100, 76, 0, .75)["decision"] == "skip_translation"
    assert language.majority_decision(100, 75, 0, .75)["decision"] == "translate"
    assert language.majority_decision(100, 70, 20, .75) == {
        "decision": "unresolved", "english_share_lower": .7, "english_share_upper": .9,
    }


def test_mainly_english_mixed_report_skips_glossary_and_keeps_minority(monkeypatch):
    module = import_module("multi_hazard_pipeline.agents.translation_agent")
    monkeypatch.setattr(module, "load_translation_glossary", lambda: pytest.fail("Glossary must not be read"))
    original = source(PASSAGES["English"] * 10 + " " + PASSAGES["German"])
    original["chunks"].append(original["chunks"][0] | {"chunk_id": "c2", "text": PASSAGES["French"]})
    translated = translation_agent(NoCallsClient(), original, correction_instruction="Retain the terminology")
    assert translated["language_analysis"]["decision"] == "skip_translation"
    assert translated["chunks"][0]["source_language"] == "Mixed"
    assert translated["chunks"][1]["source_language"] == "French"
    assert all(not detail["translation_applied"] for detail in translated["language_analysis"]["chunks"].values())
    assert [chunk["translated_text"] for chunk in translated["chunks"]] == [normalize_text(chunk["text"]) for chunk in original["chunks"]]


def test_each_batch_projects_its_own_source_languages():
    original = source(PASSAGES["German"] * 17)
    original["chunks"].append(original["chunks"][0] | {
        "chunk_id": "c2", "text": (PASSAGES["French"] + " " + PASSAGES["Italian"]) * 9,
    })
    client = TranslationClient()
    translation_agent(client, original)
    assert len(client.calls) == 2
    columns = [set().union(*(set(row) for row in call["user_payload"]["terminology_mappings"])) for call in client.calls]
    assert columns == [{"English", "German"}, {"English", "French", "Italian"}]
