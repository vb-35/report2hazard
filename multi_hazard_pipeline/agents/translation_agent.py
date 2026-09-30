from __future__ import annotations

import csv
from copy import deepcopy
from pathlib import Path
from typing import Any

from ..core import normalize_text
from ..errors import PipelineError
from ..llm import ChatClient
from ..schemas import SUPPORTED_SOURCE_LANGUAGES, translation_response_schema
from .segment_agent import batch_chunks


GLOSSARY_PATH = (
    Path(__file__).resolve().parents[2] / "Translation resources" / "multi_hazard_keywords.csv"
)

TRANSLATION_PROMPT = """
Translate every supplied report chunk into English and return JSON only as
{"translations":[{"chunk_id":"...","source_language":"English|German|French|Italian","translated_text":"..."}]}.

Rules:
- Return exactly one result per chunk, in the supplied order, using the exact chunk_id.
- Detect only English, German, French, or Italian. English text must pass through unchanged except whitespace normalization; do not semantically rewrite it.
- Translate faithfully. Do not summarize, classify, omit, combine, explain, or invent information.
- Preserve named places, torrents, rivers, structures, numbers, units, dates, directions, negation, uncertainty, and causal relationships.
- Use the supplied terminology mappings as contextual guidance, not mechanical string replacement. Select the term matching the source meaning and preserve distinctions, including German Murgang (debris flow) versus Hangmure (slope debris flow).
""".strip()


def load_translation_glossary(path: Path = GLOSSARY_PATH) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames != list(SUPPORTED_SOURCE_LANGUAGES):
            raise PipelineError("translation glossary must contain English, German, French, and Italian columns")
        rows = [{language: row.get(language, "") for language in SUPPORTED_SOURCE_LANGUAGES} for row in reader]
    if len(rows) != 151:
        raise PipelineError(f"translation glossary must contain 151 mappings, found {len(rows)}")
    return rows


def validate_translation_results(payload: Any, source: dict[str, Any]) -> None:
    if not isinstance(payload, dict) or not isinstance(payload.get("translations"), list):
        raise ValueError("translation payload must be an object with a translations list")
    expected = [normalize_text(chunk["chunk_id"]) for chunk in source.get("chunks", [])]
    results = payload["translations"]
    if any(not isinstance(item, dict) for item in results):
        raise ValueError("translation result must be an object")
    returned = [normalize_text(item.get("chunk_id", "")) for item in results]
    if len(returned) != len(set(returned)):
        raise ValueError("translation contains duplicate chunk IDs")
    unknown = set(returned) - set(expected)
    if unknown:
        raise ValueError(f"translation contains unknown chunk IDs: {sorted(unknown)}")
    missing = set(expected) - set(returned)
    if missing:
        raise ValueError(f"translation is missing chunk IDs: {sorted(missing)}")
    if returned != expected:
        raise ValueError("translation chunk IDs are reordered")
    for result, chunk in zip(results, source["chunks"], strict=True):
        language = result.get("source_language")
        translated_text = normalize_text(result.get("translated_text", ""))
        if language not in SUPPORTED_SOURCE_LANGUAGES:
            raise ValueError(f"unsupported source language: {language!r}")
        if not translated_text:
            raise ValueError(f"translation for chunk {chunk['chunk_id']} is empty")
        if language == "English" and translated_text != normalize_text(chunk["text"]):
            raise ValueError(f"English chunk {chunk['chunk_id']} was rewritten")


def validate_translated_source(translated: dict[str, Any], source: dict[str, Any]) -> None:
    expected_chunks = source.get("chunks", [])
    actual_chunks = translated.get("chunks") if isinstance(translated, dict) else None
    if not isinstance(actual_chunks, list):
        raise ValueError("translated source must contain a chunks list")
    validate_translation_results(
        {
            "translations": [
                {
                    "chunk_id": chunk.get("chunk_id"),
                    "source_language": chunk.get("source_language"),
                    "translated_text": chunk.get("translated_text"),
                }
                for chunk in actual_chunks
            ]
        },
        source,
    )
    for original, chunk in zip(expected_chunks, actual_chunks, strict=True):
        if set(chunk) != set(original) | {"source_language", "translated_text"}:
            raise ValueError(f"translated chunk {original['chunk_id']} metadata fields changed")
        for key, value in original.items():
            if chunk.get(key) != value:
                raise ValueError(f"translated chunk {original['chunk_id']} changed {key}")


def translation_agent(
    client: ChatClient,
    source: dict[str, Any],
    correction_instruction: str | None = None,
) -> dict[str, Any]:
    glossary = load_translation_glossary()
    translated = deepcopy(source)
    results: list[dict[str, Any]] = []
    for batch in batch_chunks(source["chunks"], 5000):
        def validate(payload: Any) -> None:
            validate_translation_results(payload, {"chunks": batch})

        payload = client.complete_json(
            system_prompt=TRANSLATION_PROMPT,
            user_payload={
                "doc_id": source["doc_id"],
                "chunks": [
                    {"chunk_id": chunk["chunk_id"], "text": chunk["text"]}
                    for chunk in batch
                ],
                "terminology_mappings": glossary,
                "correction_instruction": normalize_text(correction_instruction or "") or None,
            },
            validate=validate,
            response_schema=translation_response_schema(),
        )
        results.extend(payload["translations"])
    validate_translation_results({"translations": results}, source)
    for chunk, result in zip(translated["chunks"], results, strict=True):
        chunk["source_language"] = result["source_language"]
        chunk["translated_text"] = normalize_text(result["translated_text"])
    validate_translated_source(translated, source)
    return translated
