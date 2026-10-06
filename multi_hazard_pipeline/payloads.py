"""Model-only projections; saved artifacts and validation keep the full source."""

import json
from copy import deepcopy
from typing import Any

from .schemas import IMMUTABLE_SEGMENT_FIELDS


def compact_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def text_payload(chunk: dict[str, Any]) -> dict[str, str]:
    result = {"text": chunk["text"]}
    if "translated_text" in chunk and chunk["translated_text"] != chunk["text"]:
        result["translated_text"] = chunk["translated_text"]
    return result


def source_payload(
    chunks: list[dict[str, Any]], language_context: dict[str, Any] | None = None,
    *, permanent_ids: bool = False,
) -> tuple[dict[str, Any], dict[str, str]]:
    documents, projected, references = [], [], {}
    document_ids, parent_ids = {}, {}
    for chunk in chunks:
        filename = chunk.get("filename", chunk.get("file", ""))
        document = (chunk.get("document_id", chunk.get("doc_id", "")), filename)
        if document not in document_ids:
            document_ids[document] = f"d{len(documents) + 1}"
            documents.append({
                "document_id": document_ids[document],
                "filename": filename,
                "source_type": chunk.get("source_type", chunk.get("source_kind", "")),
            })
        local_id = chunk["chunk_id"] if permanent_ids else f"c{len(projected) + 1}"
        references[local_id] = chunk["chunk_id"]
        item = {"chunk_id": local_id, "document_id": document_ids[document], **text_payload(chunk)}
        for key in ("page", "paragraph", "table", "table_path", "row", "cell", "char_start", "char_end", "source_language"):
            if key in chunk:
                item[key] = chunk[key]
        if "parent_chunk_id" in chunk:
            parent = chunk["parent_chunk_id"]
            parent_ids.setdefault(parent, f"p{len(parent_ids) + 1}")
            item["parent_chunk_id"] = parent_ids[parent]
        item.update((language_context or {}).get("chunks", {}).get(chunk["chunk_id"], {}))
        projected.append(item)
    return {"documents": documents, "chunks": projected}, references


def resolve_chunk_ids(payload: Any, references: dict[str, str], *, translations: bool = False) -> dict[str, Any]:
    """Resolve only IDs issued for this request, before permanent-ID validation."""
    result = deepcopy(payload)
    key = "translations" if translations else "segments"
    if not isinstance(result, dict) or not isinstance(result.get(key), list):
        raise ValueError(f"response must contain a {key} list")
    for row in result[key]:
        if not isinstance(row, dict):
            raise ValueError("response row must be an object")
        citations = [row] if translations else row.get("evidence")
        if not isinstance(citations, list):
            raise ValueError("segment evidence must be a list")
        for citation in citations:
            ref = citation.get("chunk_id") if isinstance(citation, dict) else None
            if not isinstance(ref, str) or ref not in references:
                raise ValueError(f"unknown request-local chunk ID: {ref!r}")
            citation["chunk_id"] = references[ref]
    return result


def correction_payload(
    source: dict[str, Any], previous_answer: dict[str, Any], issues: list[dict[str, Any]],
) -> dict[str, Any]:
    """Keep stable identities across correction batches and the previous complete answer."""
    rows = previous_answer.get("rows", previous_answer.get("segments", []))
    references = {citation["chunk_id"]: citation["chunk_id"]
                  for row in rows for citation in row["evidence"]}
    return {
        "review_issues": deepcopy(issues),
        "review_chunk_references": {f"c{index}": chunk["chunk_id"] for index, chunk in enumerate(source["chunks"], 1)},
        "previous_answer": {
            "rows": segment_payload(rows, review="rows" in previous_answer, references=references),
            "translations": [{key: chunk[key] for key in ("chunk_id", "source_language", "translated_text")
                              if key in chunk} for chunk in source["chunks"]],
        },
    }


def affected_segment_ids(
    issues: list[dict[str, Any]], previous_answer: dict[str, Any],
) -> set[int] | None:
    """An empty or unknown issue scope requires a full rerun."""
    known = {row["segment"] for row in previous_answer.get("rows", previous_answer.get("segments", []))}
    if not issues or any(not issue["segment_ids"] or not set(issue["segment_ids"]) <= known for issue in issues):
        return None
    return {value for issue in issues for value in issue["segment_ids"]}


def segment_payload(
    rows: list[dict[str, Any]], *, review: bool = False, references: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    fields = IMMUTABLE_SEGMENT_FIELDS + (
        ("generalized_category", "interaction_type", "sediment_transport_phase", "classification_rationale")
        if review else ()
    )
    local_ids = references if references is not None else {}
    result = []
    for row in rows:
        item = {field: deepcopy(row[field]) for field in fields}
        item["evidence"] = []
        for citation in row["evidence"]:
            permanent_id = citation["chunk_id"]
            if references is None:
                local_ids.setdefault(permanent_id, f"c{len(local_ids) + 1}")
            item["evidence"].append({"chunk_id": local_ids[permanent_id], "quote": citation["quote"]})
        result.append(item)
    return result
