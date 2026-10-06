from __future__ import annotations

import csv
from copy import deepcopy
from pathlib import Path
from typing import Any

from ..config import DEFAULT_CONFIG, PipelineConfig
from ..core import normalize_text
from ..errors import PipelineError
from ..language import analyze_language
from ..llm import ChatClient
from ..payloads import affected_segment_ids, correction_payload, resolve_chunk_ids, source_payload
from ..schemas import SOURCE_LANGUAGE_LABELS, SUPPORTED_SOURCE_LANGUAGES, translation_response_schema
from .segment_agent import batch_chunks


GLOSSARY_PATH = (
    Path(__file__).resolve().parents[2] / "Translation resources" / "multi_hazard_keywords.csv"
)

TRANSLATION_PROMPT = """
Translate every supplied report chunk into English and return JSON only as
{"translations":[{"chunk_id":"...","source_language":"English|German|French|Italian|Mixed","translated_text":"..."}]}.

Rules:
- Return exactly one result per chunk, in the supplied order, using the exact request-local chunk_id. Respect document boundaries and supplied locations.
- Use the local language hints. Resolve Unknown text only within English, German, French, or Italian; unsupported or still unresolved substantive text is an explicit error, never an invented translation.
- Translate mixed chunks as a whole, retaining their existing English passages unchanged except whitespace normalization. Label them Mixed. English text must pass through unchanged except whitespace normalization; do not semantically rewrite it.
- Translate faithfully. Do not summarize, classify, omit, combine, explain, or invent information.
- Preserve named places, torrents, rivers, structures, numbers, units, dates, directions, negation, uncertainty, and causal relationships.
- For corrections, start with review_issues and the previous_answer. Repair the supplied affected passages, using the
  complete previous chain to understand consequences. Retain valid wording within those passages. Correction chunk IDs
  are permanent source IDs shared with previous_answer; Python retains translations outside the supplied chunks.
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


def validate_translation_results(payload: Any, source: dict[str, Any], *, allow_unknown: bool = False) -> None:
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
        if language not in SOURCE_LANGUAGE_LABELS or (language == "Unknown" and not allow_unknown):
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
        allow_unknown=True,
    )
    for original, chunk in zip(expected_chunks, actual_chunks, strict=True):
        if set(chunk) != set(original) | {"source_language", "translated_text"}:
            raise ValueError(f"translated chunk {original['chunk_id']} metadata fields changed")
        for key, value in original.items():
            if chunk.get(key) != value:
                raise ValueError(f"translated chunk {original['chunk_id']} changed {key}")
        analysis = translated.get("language_analysis")
        if analysis:
            detail = analysis["chunks"][original["chunk_id"]]
            if analysis["decision"] == "skip_translation" and detail["translation_applied"]:
                raise ValueError("mainly English report must not contain applied translations")
            if not detail["translation_applied"] and chunk["translated_text"] != normalize_text(original["text"]):
                raise ValueError(f"passthrough chunk {original['chunk_id']} was rewritten")
            if detail["translation_applied"] and chunk["source_language"] == "Unknown":
                raise ValueError(f"translated chunk {original['chunk_id']} has unresolved language")


def project_glossary(glossary: list[dict[str, str]], languages: set[str]) -> list[dict[str, str]]:
    rows = []
    for row in glossary:
        sources = {name: row[name] for name in SUPPORTED_SOURCE_LANGUAGES[1:]
                   if name in languages and row[name].strip()}
        if sources:
            rows.append({"English": row["English"], **sources})
    return rows


def translation_agent(
    client: ChatClient,
    source: dict[str, Any],
    correction_instruction: str | None = None,
    *,
    config: PipelineConfig = DEFAULT_CONFIG,
    previous_analysis: dict[str, Any] | None = None,
    previous: dict[str, Any] | None = None,
    previous_answer: dict[str, Any] | None = None,
    review_issues: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    correction = correction_payload(previous or source, previous_answer or {}, review_issues) if review_issues else {}
    affected_chunks = None
    if previous is not None and review_issues and previous_answer:
        try:
            validate_translated_source(previous, source)
        except ValueError:
            pass  # Invalid prior translations require a full rerun.
        else:
            affected = affected_segment_ids([issue for issue in review_issues if issue["stage"] == "translation"], previous_answer)
            rows = [row for row in previous_answer.get("rows", []) if affected and row["segment"] in affected]
            source_by_id = {chunk["chunk_id"]: normalize_text(chunk["text"]).casefold() for chunk in source["chunks"]}
            citations = [citation for row in rows for citation in row["evidence"]]
            if citations and all(row["evidence"] for row in rows) and all(
                                 normalize_text(citation["quote"]) and citation["chunk_id"] in source_by_id and normalize_text(citation["quote"]).casefold()
                                 in source_by_id[citation["chunk_id"]] for citation in citations):
                affected_chunks = {citation["chunk_id"] for citation in citations}
    analysis = analyze_language(source, config, previous_analysis)
    translated = deepcopy(source)
    translated["language_analysis"] = analysis
    pending = []
    previous_by_id = {chunk["chunk_id"]: chunk for chunk in (previous or {}).get("chunks", [])}
    for original, chunk in zip(source["chunks"], translated["chunks"], strict=True):
        detail = analysis["chunks"][chunk["chunk_id"]]
        chunk["source_language"] = detail["source_language"]
        chunk["translated_text"] = normalize_text(chunk["text"])
        if (analysis["decision"] == "translate" and detail["total_alphabetic_count"]
                and detail["source_language"] != "English"):
            if affected_chunks is not None and chunk["chunk_id"] not in affected_chunks:
                prior = previous_by_id[chunk["chunk_id"]]
                chunk["source_language"] = prior["source_language"]
                chunk["translated_text"] = prior["translated_text"]
                detail["translation_applied"] = (previous.get("language_analysis", {}).get("chunks", {})
                                                 .get(chunk["chunk_id"], {}).get("translation_applied", True))
                continue
            pending.append(original)
    glossary = load_translation_glossary() if pending else []
    by_id = {chunk["chunk_id"]: chunk for chunk in translated["chunks"]}
    for batch in batch_chunks(pending, 5000):
        projected, references = source_payload(batch, permanent_ids=bool(review_issues))
        for item, chunk in zip(projected["chunks"], batch, strict=True):
            detail = analysis["chunks"][chunk["chunk_id"]]
            item.update(source_language=detail["source_language"], languages=detail["languages"])

        def validate(payload: Any) -> None:
            payload = resolve_chunk_ids(payload, references, translations=True)
            validate_translation_results(payload, {"chunks": batch})
            for result in payload["translations"]:
                hint = analysis["chunks"][normalize_text(result["chunk_id"])]["source_language"]
                if hint != "Unknown" and result["source_language"] != hint:
                    raise ValueError(f"translation changed detected language for {result['chunk_id']}")

        languages = set()
        for chunk in batch:
            detail = analysis["chunks"][chunk["chunk_id"]]
            languages.update(detail["languages"])
            if detail["unresolved_count"]:
                languages.update(SUPPORTED_SOURCE_LANGUAGES[1:])

        payload = client.complete_json(
            system_prompt=TRANSLATION_PROMPT,
            user_payload={
                **projected,
                **correction,
                "terminology_mappings": project_glossary(glossary, languages),
                "correction_instruction": normalize_text(correction_instruction or "") or None,
            },
            validate=validate,
            response_schema=translation_response_schema(),
        )
        validate(payload)
        payload = resolve_chunk_ids(payload, references, translations=True)
        analysis["translation_request_count"] += 1
        for result in payload["translations"]:
            chunk = by_id[normalize_text(result["chunk_id"])]
            chunk["source_language"] = result["source_language"]
            chunk["translated_text"] = normalize_text(result["translated_text"])
            analysis["chunks"][chunk["chunk_id"]]["translation_applied"] = True
    validate_translated_source(translated, source)
    return translated
