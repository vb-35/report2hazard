from __future__ import annotations

from copy import deepcopy
from typing import Any

from ..config import PipelineConfig
from ..core import normalize_text
from ..errors import PipelineError
from ..language import language_prompt_context
from ..llm import ChatClient
from ..schemas import (
    CitationQuoteMismatch,
    citation_verification_response_schema,
    consolidation_response_schema,
    segment_response_schema,
    validate_segment_chain,
)


SEGMENTATION_PROMPT = """
Define one coherent ordered causal chain for the complete qualitative hazard report. Return JSON only as
{"segments":[{"segment":1,"causal_order":1,"predecessor_segment_ids":[],"event":"...","process":"...","evidence":[{"chunk_id":"...","quote":"..."}]}]}.

Rules:
- A segment is one distinct causal-process step: what happened, why it happened, or what it caused. It is not every sentence, number, observation, or location.
- Order segments by causal order, not document order. Use contiguous causal_order values beginning at 1. segment is a positive integer ID; Python preserves IDs for unchanged steps across revisions.
- predecessor_segment_ids may be empty; otherwise cite only real, earlier segment numbers that directly enable the step.
- Every segment needs at least one evidence citation. Use only supplied chunk IDs and copy an exact quote or a whitespace-normalized substring from that chunk.
- Use translated_text for semantic interpretation. When translation_applied is false, this is original-language passthrough: interpret all minority-language passages directly, including German, French, Italian, and mixed text. Do not omit them. event and process must always be written in English.
- Evidence quote must remain in the original source language: copy it only from the chunk's text field, never from translated_text.
- PDF extraction may contain replacement characters (�) or broken words. Quote a short contiguous fragment exactly as supplied in text; do not repair its spelling or accents in the quote.
- Merge repeated descriptions and quantities for the same causal step and retain every useful citation. Merge a cause with its immediate consequence only when the reference abstraction treats them as one statement.
- Separate materially different causal steps, especially when causal role, structure interaction, or erosion/transport/deposition/connectivity changes.
- Reconcile facts across the whole input. Do not invent a step or evidence to reach a desired count. An empty batch may return an empty segments list.
- Keep event and process concise. Preserve named torrents, rivers, structures, and causal meaning.

Representative Schnannerbach example:
Input chunks: c1="Previous landslide deposits were remobilized into the main channel. Feeder channels delivered large sediment volumes into Schnannerbach.";
c2="The sediment retention basin filled and was overtopped by debris flow.";
c3="The bridge near the Rosanna River became blocked by debris. Backflow from the Rosanna River caused upstream flooding."
Output: {"segments":[
 {"segment":1,"causal_order":1,"predecessor_segment_ids":[],"event":"Schnannerbach","process":"Previous landslide deposits remobilized into the main channel","evidence":[{"chunk_id":"c1","quote":"Previous landslide deposits were remobilized into the main channel"}]},
 {"segment":2,"causal_order":2,"predecessor_segment_ids":[1],"event":"Schnannerbach","process":"Feeder channels delivered large sediment volumes into Schnannerbach","evidence":[{"chunk_id":"c1","quote":"Feeder channels delivered large sediment volumes into Schnannerbach"}]},
 {"segment":3,"causal_order":3,"predecessor_segment_ids":[2],"event":"Schnannerbach","process":"Sediment retention basin filled and was overtopped by debris flow","evidence":[{"chunk_id":"c2","quote":"The sediment retention basin filled and was overtopped by debris flow"}]},
 {"segment":4,"causal_order":4,"predecessor_segment_ids":[3],"event":"Schnannerbach","process":"Bridge near Rosanna River became blocked by debris","evidence":[{"chunk_id":"c3","quote":"The bridge near the Rosanna River became blocked by debris"}]},
 {"segment":5,"causal_order":5,"predecessor_segment_ids":[4],"event":"Schnannerbach","process":"Backflow from Rosanna River caused upstream flooding","evidence":[{"chunk_id":"c3","quote":"Backflow from the Rosanna River caused upstream flooding"}]}
]}
""".strip()


CONSOLIDATION_PROMPT = """
Consolidate the supplied batch-level causal steps into one coherent report-level chain. Return JSON only with segments.
For each output segment, provide segment, causal_order, predecessor_segment_ids, event, process, and source_segment_ids.
Use contiguous segment and causal_order values beginning at 1. Each source_segment_id must appear exactly once across
the entire output; combine IDs only when they describe the same causal step. Preserve every distinct causal step,
order them by causality, and use only earlier output segment numbers as predecessors. Write event and process in English.
Do not return evidence quotes: Python attaches every original citation from the listed source_segment_ids.
Do not invent facts or source IDs.
""".strip()


CITATION_VERIFICATION_PROMPT = """
Check one nonliteral evidence citation against short fragments of the original extracted source text. The cited wording
may join text from two adjacent pages or from noncontinuous table cells. Decide whether the selected fragments together
actually support the segment's process in meaning, not merely share keywords. Do not infer facts absent from the source.
If supported, return supported=true and the smallest set of 1-3 fragment_ids that substantiate it. Fragments may come
from the cited page and its immediate neighbors. If not clearly supported, return supported=false and fragment_ids=[].
Return only JSON with supported, fragment_ids, and a concise reason. Python will cite the exact selected source fragments.
""".strip()


def batch_chunks(chunks: list[dict[str, Any]], max_chars: int) -> list[list[dict[str, Any]]]:
    if max_chars < 1:
        raise PipelineError("batch character limit must be positive")
    batches: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    current_size = 0
    for chunk in chunks:
        size = len(chunk["text"]) + len(chunk.get("translated_text", ""))
        if size > max_chars:
            raise PipelineError(
                f"chunk {chunk['chunk_id']} contains {size} text characters, exceeding the "
                f"{max_chars} batch limit; re-extract with smaller source chunks or increase the limit"
            )
        if current and current_size + size > max_chars:
            batches.append(current)
            current, current_size = [], 0
        current.append(chunk)
        current_size += size
    if current:
        batches.append(current)
    return batches


def select_semantic_chunks(chunks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Compatibility hook: every source chunk is now semantically eligible."""
    return chunks


def _normalized_citation(citation: dict[str, Any]) -> dict[str, str]:
    return {"chunk_id": normalize_text(citation["chunk_id"]), "quote": normalize_text(citation["quote"])}


def _reindex_chain(items: list[dict[str, Any]], start: int = 1) -> list[dict[str, Any]]:
    ordered = sorted(items, key=lambda item: item["causal_order"])
    mapping = {item["segment"]: start + index for index, item in enumerate(ordered)}
    result: list[dict[str, Any]] = []
    for index, item in enumerate(ordered):
        result.append(
            {
                "segment": start + index,
                "causal_order": start + index,
                "predecessor_segment_ids": [mapping[value] for value in item["predecessor_segment_ids"]],
                "event": normalize_text(item["event"]),
                "process": normalize_text(item["process"]),
                "evidence": [_normalized_citation(citation) for citation in item["evidence"]],
            }
        )
    return result


def consolidate_exact_duplicates(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Merge exact normalized event/process duplicates without losing citations."""
    if not items:
        return []
    kept: list[dict[str, Any]] = []
    by_key: dict[tuple[str, str], dict[str, Any]] = {}
    old_to_kept: dict[int, int] = {}
    for item in sorted(deepcopy(items), key=lambda value: value["causal_order"]):
        key = (normalize_text(item["event"]).casefold(), normalize_text(item["process"]).casefold())
        existing = by_key.get(key)
        if existing is None:
            by_key[key] = item
            kept.append(item)
            old_to_kept[item["segment"]] = item["segment"]
            continue
        old_to_kept[item["segment"]] = existing["segment"]
        citations = {(value["chunk_id"], value["quote"]) for value in existing["evidence"]}
        for citation in item["evidence"]:
            normalized = _normalized_citation(citation)
            key_citation = (normalized["chunk_id"], normalized["quote"])
            if key_citation not in citations:
                existing["evidence"].append(normalized)
                citations.add(key_citation)
    for item in kept:
        item["predecessor_segment_ids"] = list(
            dict.fromkeys(
                old_to_kept.get(value, value)
                for value in item["predecessor_segment_ids"]
                if old_to_kept.get(value, value) != item["segment"]
            )
        )
    return _reindex_chain(kept)


def _restore_consolidated_evidence(
    payload: Any, preliminary: list[dict[str, Any]], source: dict[str, Any]
) -> list[dict[str, Any]]:
    if not isinstance(payload, dict) or not isinstance(payload.get("segments"), list):
        raise ValueError("consolidation must return a segments list")
    by_id = {item["segment"]: item for item in preliminary}
    used: list[int] = []
    result: list[dict[str, Any]] = []
    for row in payload["segments"]:
        if not isinstance(row, dict) or not isinstance(row.get("source_segment_ids"), list) or not row["source_segment_ids"]:
            raise ValueError("each consolidated segment needs source_segment_ids")
        citations: list[dict[str, str]] = []
        for source_id in row["source_segment_ids"]:
            if type(source_id) is not int or source_id not in by_id:
                raise ValueError(f"consolidation cites unknown source segment {source_id}")
            used.append(source_id)
            for citation in by_id[source_id]["evidence"]:
                if citation not in citations:
                    citations.append(deepcopy(citation))
        result.append({key: row[key] for key in ("segment", "causal_order", "predecessor_segment_ids", "event", "process")} | {"evidence": citations})
    missing = set(by_id) - set(used)
    if missing:
        detail = "; ".join(f"{source_id}: {by_id[source_id]['evidence'][0]['quote']!r}" for source_id in sorted(missing))
        raise ValueError(f"consolidation dropped source segment(s) and evidence citation(s): {detail}")
    if len(used) != len(set(used)):
        raise ValueError("consolidation used a source segment more than once")
    validate_segment_chain({"segments": result}, source)
    return result


def _verify_nonliteral_citation(
    client: ChatClient,
    item: dict[str, Any],
    mismatch: CitationQuoteMismatch,
    chunks: list[dict[str, Any]],
) -> list[dict[str, str]]:
    source_index = next(index for index, chunk in enumerate(chunks) if normalize_text(chunk["chunk_id"]) == mismatch.chunk_id)
    source_file = chunks[source_index].get("filename")
    fragments: list[dict[str, str]] = []
    for index in range(max(0, source_index - 1), min(len(chunks), source_index + 2)):
        chunk = chunks[index]
        if chunk.get("filename") != source_file:
            continue
        content = normalize_text(chunk["text"])
        for start in range(0, len(content), 250):
            fragment = content[start:start + 350]
            if fragment and (len(fragment) >= 40 or start == 0):
                fragments.append({
                    "fragment_id": f"f{index}-{start}",
                    "chunk_id": chunk["chunk_id"],
                    "text": fragment,
                })
    by_id = {fragment["fragment_id"]: fragment for fragment in fragments}

    def validate(response: Any) -> None:
        if not isinstance(response, dict) or type(response.get("supported")) is not bool:
            raise ValueError("citation verification needs a supported boolean")
        if not isinstance(response.get("reason"), str):
            raise ValueError("citation verification needs a reason")
        ids = response.get("fragment_ids")
        if not isinstance(ids, list) or any(not isinstance(value, str) or value not in by_id for value in ids):
            raise ValueError("citation verification returned unknown fragment IDs")
        if len(ids) != len(set(ids)) or (response["supported"] and not 1 <= len(ids) <= 3):
            raise ValueError("citation verification needs 1-3 unique supporting fragments")
        if not response["supported"] and ids:
            raise ValueError("unsupported citations must not select fragments")

    response = client.complete_json(
        system_prompt=CITATION_VERIFICATION_PROMPT,
        user_payload={
            "event": item["event"], "process": item["process"],
            "proposed_quote": mismatch.quote, "cited_chunk_id": mismatch.chunk_id,
            "fragments": fragments,
        },
        validate=validate,
        response_schema=citation_verification_response_schema(),
    )
    if not response["supported"]:
        raise ValueError(f"segment {mismatch.segment} citation is not supported by the cited or adjacent source pages")
    return [
        {"chunk_id": by_id[value]["chunk_id"], "quote": by_id[value]["text"]}
        for value in response["fragment_ids"]
    ]


def stabilize_segment_ids(
    revised: dict[str, Any], previous: dict[str, Any]
) -> dict[str, Any]:
    """Retain IDs for unchanged causal steps while allowing order to change."""
    result = deepcopy(revised)
    previous_by_key = {
        (normalize_text(item["event"]).casefold(), normalize_text(item["process"]).casefold()): int(
            item["segment"]
        )
        for item in previous["segments"]
    }
    next_id = max((int(item["segment"]) for item in previous["segments"]), default=0) + 1
    assigned: set[int] = set()
    mapping: dict[int, int] = {}
    for item in sorted(result["segments"], key=lambda value: value["causal_order"]):
        key = (normalize_text(item["event"]).casefold(), normalize_text(item["process"]).casefold())
        stable_id = previous_by_key.get(key)
        if stable_id is None or stable_id in assigned:
            stable_id = next_id
            next_id += 1
        mapping[int(item["segment"])] = stable_id
        assigned.add(stable_id)
    for item in result["segments"]:
        original_id = int(item["segment"])
        item["segment"] = mapping[original_id]
        item["predecessor_segment_ids"] = [mapping[int(value)] for value in item["predecessor_segment_ids"]]
    return result


def collect_segments_from_chunks(
    client: ChatClient,
    *,
    doc_id: str,
    chunks: list[dict[str, Any]],
    segments: list[dict[str, Any]],
    seen: set[tuple[str, str]] | None = None,
    max_chars: int,
    starting_batch_index: int = 1,
    correction_instruction: str | None = None,
    language_context: dict[str, Any] | None = None,
) -> int:
    batch_index = starting_batch_index
    for batch in batch_chunks(chunks, max_chars):
        def validate(payload: Any) -> None:
            batch_ids = {normalize_text(chunk["chunk_id"]) for chunk in batch}
            for item in payload["segments"]:
                for citation in item["evidence"]:
                    if normalize_text(citation["chunk_id"]) not in batch_ids:
                        raise ValueError(f"segment {item['segment']} cites unknown chunk {citation['chunk_id']}")
            for _ in range(sum(len(item["evidence"]) for item in payload["segments"]) + 1):
                try:
                    validate_segment_chain(payload, {"chunks": chunks})
                    return
                except CitationQuoteMismatch as mismatch:
                    item = next(value for value in payload["segments"] if value["segment"] == mismatch.segment)
                    index = next(
                        index for index, citation in enumerate(item["evidence"])
                        if normalize_text(citation["chunk_id"]) == mismatch.chunk_id
                        and normalize_text(citation["quote"]) == mismatch.quote
                    )
                    item["evidence"][index:index + 1] = _verify_nonliteral_citation(client, item, mismatch, chunks)
            raise ValueError("citation verification did not resolve all nonliteral quotes")

        payload = client.complete_json(
            system_prompt=SEGMENTATION_PROMPT,
            user_payload={
                "doc_id": doc_id,
                "batch_index": batch_index,
                "chunks": batch,
                "language_context": {
                    "decision": (language_context or {}).get("decision", "legacy_english_translation"),
                    "chunks": {chunk["chunk_id"]: (language_context or {}).get("chunks", {}).get(chunk["chunk_id"], {})
                               for chunk in batch},
                },
                "correction_instruction": normalize_text(correction_instruction or "") or None,
            },
            validate=validate,
            response_schema=segment_response_schema(),
        )
        reindexed = _reindex_chain(payload["segments"], len(segments) + 1)
        segments.extend(reindexed)
        batch_index += 1
    return batch_index


def segment_agent(
    client: ChatClient,
    source: dict[str, Any],
    config: PipelineConfig,
    correction_instruction: str | None = None,
) -> dict[str, Any]:
    chunks = select_semantic_chunks(source["chunks"])
    max_chars = max(1, config.batch_max_chars // 2) if correction_instruction else config.batch_max_chars
    batches = batch_chunks(chunks, max_chars)
    preliminary: list[dict[str, Any]] = []
    collect_segments_from_chunks(
        client,
        doc_id=source["doc_id"],
        chunks=chunks,
        segments=preliminary,
        max_chars=max_chars,
        correction_instruction=correction_instruction,
        language_context=language_prompt_context(source),
    )
    preliminary = consolidate_exact_duplicates(preliminary)
    validate_segment_chain({"segments": preliminary}, source)

    segments = preliminary
    if len(batches) > 1:
        def validate_consolidated(payload: Any) -> None:
            _restore_consolidated_evidence(payload, preliminary, source)

        consolidated = client.complete_json(
            system_prompt=CONSOLIDATION_PROMPT,
            user_payload={
                "doc_id": source["doc_id"],
                "batch_segments": preliminary,
                "correction_instruction": normalize_text(correction_instruction or "") or None,
            },
            validate=validate_consolidated,
            response_schema=consolidation_response_schema(),
        )
        segments = consolidate_exact_duplicates(_restore_consolidated_evidence(consolidated, preliminary, source))
        validate_segment_chain({"segments": segments}, source)

    if not segments:
        raise PipelineError("segment agent produced no causal-process segments")
    return {"doc_id": source["doc_id"], "status": "pass", "segments": segments}
