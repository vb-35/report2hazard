from __future__ import annotations

import json
import os
import re
import tempfile
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from pypdf import PdfReader, PdfWriter

from .config import DEFAULT_CONFIG, PipelineConfig
from .core import normalize_text
from .errors import PipelineError
from .llm import ChatClient
from .schemas import event_separation_response_schema


EVENT_SEPARATION_PROMPT = """Identify starts of independent event reports in a PDF collection.
An event report is a self-contained report about one occurrence or one connected multi-hazard occurrence.
Multiple hazards do not automatically mean multiple events. Flooding, erosion, sediment transport,
debris flow, landslide, and damage may belong to one causal event. Geographic headings such as Tirol,
Steiermark, or Kärnten are containers, not event reports. Internal subsections such as Beschreibung des
Einzugsgebietes, Ereignisbeschreibung, Schäden, Sofortmaßnahmen, Fotodokumentation, and
Niederschlagsanalyse belong to the surrounding event report. TOC occurrences are never sufficient
evidence of an actual start: every valid start needs a heading_quote from the actual content page.
Overview statistics, meteorological summaries, introductions, conclusions, tables of figures, and
bibliographies are not event reports. Return verified starts, not page ranges, and identify the first page
after the event-report collection when present. Never invent titles, events, dates, locations, or
boundaries. Return status review_required for ambiguous structure, including two reports starting on one
physical page. Page numbers are physical 1-based PDF pages.

The input encodes outline rows as [hierarchy_level, physical_page, bookmark_title] and page_context rows
as [physical_page, extracted_text]. Return exactly one JSON object with these keys and no others:
{"status":"pass","events":[{"title":"visible heading title","start_page":1,
"heading_quote":"exact visible heading","confidence":0.99}],"collection_end":{"page":10,
"heading_quote":"exact first post-collection heading"},"warnings":[]}. collection_end may be null.
Do not return a bare array. Do not put the collection-end heading in events."""

TOC_HEADING = re.compile(
    r"^(?:inhalt|inhaltsverzeichnis|table\s+of\s+contents|contents|sommaire|indice)$",
    re.IGNORECASE,
)
TOC_ENTRY = re.compile(r"^\s*(.+?)\s*(?:\.{2,}|\s{2,})\s*(\d+)\s*$")
NUMBERING = re.compile(r"^\s*(?:\d+(?:\.\d+)*\.?|[IVXLCDM]+\.?)\s*", re.IGNORECASE)
MIN_CONFIDENCE = 0.8


@dataclass(frozen=True)
class EventRange:
    sequence: int
    title: str
    start_page: int
    end_page: int


def extract_outline(reader: PdfReader) -> list[dict[str, Any]]:
    """Return the PDF outline as nested nodes with physical 1-based destinations."""

    def walk(items: list[Any]) -> list[dict[str, Any]]:
        nodes: list[dict[str, Any]] = []
        for item in items:
            if isinstance(item, list):
                if nodes:
                    nodes[-1]["children"] = walk(item)
                continue
            try:
                page = reader.get_destination_page_number(item) + 1
            except Exception:
                page = None
            nodes.append(
                {
                    "title": str(getattr(item, "title", item)),
                    "page": page,
                    "children": [],
                }
            )
        return nodes

    return walk(list(reader.outline))


def flatten_outline(nodes: list[dict[str, Any]], level: int = 0) -> list[dict[str, Any]]:
    flattened: list[dict[str, Any]] = []
    for node in nodes:
        flattened.append({"level": level, "title": node["title"], "page": node["page"]})
        flattened.extend(flatten_outline(node["children"], level + 1))
    return flattened


def parse_toc(page_texts: list[str]) -> tuple[set[int], list[dict[str, Any]]]:
    toc_pages: set[int] = set()
    candidates: list[dict[str, Any]] = []
    active = False
    for page, text in enumerate(page_texts, start=1):
        lines = [normalize_text(line) for line in text.splitlines() if normalize_text(line)]
        has_heading = any(TOC_HEADING.fullmatch(line) for line in lines)
        if has_heading:
            active = True
        entries = []
        if active:
            for line in lines:
                match = TOC_ENTRY.match(line)
                if match:
                    entries.append(
                        {
                            "title": normalize_text(match.group(1)),
                            "printed_page": int(match.group(2)),
                            "toc_page": page,
                        }
                    )
        dotted_lines = sum("..." in line for line in lines)
        if active and (has_heading or entries or dotted_lines >= 2):
            toc_pages.add(page)
            candidates.extend(entries)
        elif active:
            active = False
    return toc_pages, candidates


def _heading_key(text: str) -> str:
    return normalize_text(NUMBERING.sub("", text)).casefold().strip(" .:-")


def resolve_toc_candidates(
    candidates: list[dict[str, Any]], page_texts: list[str], toc_pages: set[int]
) -> list[dict[str, Any]]:
    resolved = []
    page_lines = {
        page: {_heading_key(line) for line in text.splitlines() if _heading_key(line)}
        for page, text in enumerate(page_texts, start=1)
        if page not in toc_pages
    }
    for candidate in candidates:
        key = _heading_key(candidate["title"])
        matches = [page for page, lines in page_lines.items() if key and key in lines]
        resolved.append(candidate | {"physical_page": matches[0] if len(matches) == 1 else None})
    return resolved


def _quote_on_page(quote: str, text: str) -> bool:
    return normalize_text(quote).casefold() in normalize_text(text).casefold()


def validate_separation(
    payload: Any,
    page_texts: list[str],
    toc_pages: set[int] | None = None,
    *,
    allow_empty: bool = False,
) -> None:
    required = {"status", "events", "collection_end", "warnings"}
    if not isinstance(payload, dict) or set(payload) != required:
        raise ValueError("separation response fields are invalid")
    if payload.get("status") not in {"pass", "review_required"}:
        raise ValueError("separation status must be pass or review_required")
    if not isinstance(payload.get("events"), list) or not isinstance(payload.get("warnings"), list):
        raise ValueError("separation response must include events and warnings lists")
    if any(not isinstance(warning, str) for warning in payload["warnings"]):
        raise ValueError("separation warnings must be strings")
    if payload["status"] == "review_required":
        return
    if not payload["events"] and not allow_empty:
        raise ValueError("no event-report starts were verified")
    seen: set[int] = set()
    for event in payload["events"]:
        if not isinstance(event, dict) or set(event) != {
            "title", "start_page", "heading_quote", "confidence"
        }:
            raise ValueError("event must be an object")
        page = event.get("start_page")
        if isinstance(page, bool) or not isinstance(page, int) or not 1 <= page <= len(page_texts):
            raise ValueError(f"event start page is outside the PDF: {page!r}")
        if page in seen:
            raise ValueError(f"multiple event reports start on physical page {page}")
        if toc_pages and page in toc_pages:
            raise ValueError(f"TOC page {page} cannot be an event-report start")
        seen.add(page)
        title = event.get("title", "")
        if not normalize_text(title):
            raise ValueError("event title is required")
        if not _quote_on_page(title, page_texts[page - 1]):
            raise ValueError(f"event title does not occur on physical page {page}")
        confidence = event.get("confidence")
        if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not 0 <= confidence <= 1:
            raise ValueError("event confidence must be between 0 and 1")
        if confidence < MIN_CONFIDENCE:
            raise ValueError(f"event start on page {page} is not reliable")
        quote = event.get("heading_quote", "")
        if not normalize_text(quote) or not _quote_on_page(quote, page_texts[page - 1]):
            raise ValueError(f"heading quote does not occur on physical page {page}")
    boundary = payload.get("collection_end")
    if boundary is not None:
        if (
            not isinstance(boundary, dict)
            or set(boundary) != {"page", "heading_quote"}
            or isinstance(boundary.get("page"), bool)
            or not isinstance(boundary.get("page"), int)
        ):
            raise ValueError("collection_end must contain a physical page")
        page = boundary["page"]
        if not 1 <= page <= len(page_texts):
            raise ValueError("collection_end page is outside the PDF")
        quote = boundary.get("heading_quote", "")
        if not normalize_text(quote) or not _quote_on_page(quote, page_texts[page - 1]):
            raise ValueError(f"collection-end quote does not occur on physical page {page}")


def construct_ranges(
    events: list[dict[str, Any]], collection_end: dict[str, Any] | None, page_count: int
) -> list[EventRange]:
    starts = sorted(events, key=lambda event: event["start_page"])
    if len({event["start_page"] for event in starts}) != len(starts):
        raise PipelineError("multiple event reports start on the same physical page; review required")
    final_end = collection_end["page"] - 1 if collection_end else page_count
    ranges = [
        EventRange(
            sequence=index,
            title=event["title"],
            start_page=event["start_page"],
            end_page=(starts[index]["start_page"] - 1 if index < len(starts) else final_end),
        )
        for index, event in enumerate(starts, start=1)
    ]
    validate_ranges(ranges, page_count)
    return ranges


def validate_ranges(ranges: Iterable[EventRange], page_count: int) -> None:
    previous_end = 0
    seen: set[tuple[int, int]] = set()
    for event in ranges:
        if not 1 <= event.start_page <= event.end_page <= page_count:
            raise PipelineError(
                f"invalid event range {event.start_page}-{event.end_page} for {page_count}-page PDF"
            )
        if event.start_page <= previous_end:
            raise PipelineError("event ranges must be ordered and non-overlapping")
        key = (event.start_page, event.end_page)
        if key in seen:
            raise PipelineError("duplicate event range")
        seen.add(key)
        previous_end = event.end_page


def safe_event_filename(event: EventRange, page_count: int) -> str:
    ascii_title = unicodedata.normalize("NFKD", event.title).encode("ascii", "ignore").decode()
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_title.casefold()).strip("-") or "event"
    width = max(3, len(str(page_count)))
    return (
        f"{event.sequence:02d}-{slug}_pages-"
        f"{event.start_page:0{width}d}-{event.end_page:0{width}d}.pdf"
    )


def _candidate_payload(
    outline: list[dict[str, Any]],
    toc: list[dict[str, Any]],
    page_texts: list[str],
    max_chars: int,
) -> dict[str, Any]:
    flat = flatten_outline(outline)
    structural = {
        node["page"]
        for node in flat
        if node["page"] is not None
        and (node["level"] == 0 or any(
            parent["page"] == node["page"] and parent["children"]
            for parent in _walk_nodes(outline)
        ))
    }
    structural.update(item["physical_page"] for item in toc if item.get("physical_page"))
    pages = sorted(structural)
    compact_outline = [
        [item["level"], item["page"], item["title"][:120]] for item in flat
    ]
    context_pages: set[int] = set()
    for page in pages:
        for context_page in range(max(1, page - 1), min(len(page_texts), page + 1) + 1):
            context_pages.add(context_page)
    base = {
        "outline": compact_outline,
        "toc_candidates": toc,
        "candidate_pages": pages,
        "page_context": [],
        "instruction": "Verify actual event starts and the first post-collection section.",
    }
    candidate_chars = 240
    neighbor_chars = 48
    while True:
        base["page_context"] = [
            [
                page,
                page_texts[page - 1][
                    : candidate_chars if page in structural else neighbor_chars
                ],
            ]
            for page in sorted(context_pages)
        ]
        if len(json.dumps(base, ensure_ascii=False)) <= max_chars or candidate_chars == 1:
            break
        candidate_chars = max(1, candidate_chars * 4 // 5)
        neighbor_chars = max(1, neighbor_chars * 4 // 5)
    return base


def _walk_nodes(nodes: list[dict[str, Any]]) -> Iterable[dict[str, Any]]:
    for node in nodes:
        yield node
        yield from _walk_nodes(node["children"])


def _page_batches(page_texts: list[str], max_chars: int) -> Iterable[list[dict[str, Any]]]:
    start = 0
    while start < len(page_texts):
        batch: list[dict[str, Any]] = []
        used = 0
        index = start
        while index < len(page_texts):
            text = page_texts[index]
            remaining = max_chars - used
            if batch and len(text) > remaining:
                break
            clipped = text[: max(1, remaining)]
            batch.append({"page": index + 1, "text": clipped})
            used += len(clipped)
            index += 1
            if used >= max_chars:
                break
        yield batch
        if index >= len(page_texts):
            break
        start = max(start + 1, index - 1)


def _ask(client: ChatClient, payload: dict[str, Any], page_texts: list[str], toc_pages: set[int], *, allow_empty: bool) -> dict[str, Any]:
    return client.complete_json(
        system_prompt=EVENT_SEPARATION_PROMPT,
        user_payload=payload,
        response_schema=event_separation_response_schema(),
        validate=lambda response: validate_separation(
            response, page_texts, toc_pages, allow_empty=allow_empty
        ),
    )


def _detect_boundaries(
    client: ChatClient,
    config: PipelineConfig,
    outline: list[dict[str, Any]],
    toc: list[dict[str, Any]],
    toc_pages: set[int],
    page_texts: list[str],
) -> dict[str, Any]:
    if outline or toc:
        return _ask(
            client,
            _candidate_payload(outline, toc, page_texts, config.batch_max_chars),
            page_texts,
            toc_pages,
            allow_empty=False,
        )
    responses = [
        _ask(client, {"pages": batch}, page_texts, toc_pages, allow_empty=True)
        for batch in _page_batches(page_texts, config.batch_max_chars)
    ]
    if any(response["status"] == "review_required" for response in responses):
        return {"status": "review_required", "events": [], "collection_end": None, "warnings": [
            warning for response in responses for warning in response["warnings"]
        ]}
    by_page: dict[int, dict[str, Any]] = {}
    for response in responses:
        for event in response["events"]:
            current = by_page.get(event["start_page"])
            if current is None or event["confidence"] > current["confidence"]:
                by_page[event["start_page"]] = event
    boundaries = [response["collection_end"] for response in responses if response["collection_end"]]
    result = {
        "status": "pass",
        "events": list(by_page.values()),
        "collection_end": min(boundaries, key=lambda item: item["page"]) if boundaries else None,
        "warnings": [warning for response in responses for warning in response["warnings"]],
    }
    validate_separation(result, page_texts, toc_pages)
    return result


def _write_ranges(input_pdf: Path, output_dir: Path, ranges: list[EventRange]) -> list[Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    reader = PdfReader(str(input_pdf))
    outputs = [output_dir / safe_event_filename(event, len(reader.pages)) for event in ranges]
    temporary: list[Path] = []
    committed: list[Path] = []
    try:
        for output, event in zip(outputs, ranges, strict=True):
            writer = PdfWriter()
            for page_index in range(event.start_page - 1, event.end_page):
                writer.add_page(reader.pages[page_index])
            handle = tempfile.NamedTemporaryFile(
                mode="wb", prefix=f".{output.stem}-", suffix=".tmp", dir=output_dir, delete=False
            )
            temp_path = Path(handle.name)
            temporary.append(temp_path)
            with handle:
                writer.write(handle)
        for temp_path, output in zip(temporary, outputs, strict=True):
            os.replace(temp_path, output)
            committed.append(output)
        return outputs
    except Exception as exc:
        for path in temporary:
            path.unlink(missing_ok=True)
        for path in committed:
            path.unlink(missing_ok=True)
        raise PipelineError(f"could not write split event PDFs: {exc}") from exc


def split_event_reports(
    input_pdf: Path,
    output_dir: Path,
    client: ChatClient,
    config: PipelineConfig = DEFAULT_CONFIG,
) -> list[Path]:
    input_pdf = Path(input_pdf).resolve()
    output_dir = Path(output_dir).resolve()
    if not input_pdf.is_file() or input_pdf.suffix.lower() != ".pdf":
        raise PipelineError(f"input is not a PDF file: {input_pdf}")
    try:
        reader = PdfReader(str(input_pdf))
        if not reader.pages:
            raise PipelineError("input PDF has no pages")
        page_texts = [page.extract_text() or "" for page in reader.pages]
        outline = extract_outline(reader)
    except PipelineError:
        raise
    except Exception as exc:
        raise PipelineError(f"could not inspect input PDF: {exc}") from exc
    toc_pages, toc_candidates = parse_toc(page_texts)
    toc = resolve_toc_candidates(toc_candidates, page_texts, toc_pages)
    result = _detect_boundaries(client, config, outline, toc, toc_pages, page_texts)
    if result["status"] != "pass":
        warning = "; ".join(result["warnings"]) or "ambiguous event-report boundaries"
        raise PipelineError(f"event separation requires review: {warning}")
    try:
        validate_separation(result, page_texts, toc_pages)
    except ValueError as exc:
        raise PipelineError(f"invalid event separation: {exc}") from exc
    ranges = construct_ranges(result["events"], result["collection_end"], len(reader.pages))
    if len(ranges) == 1 and ranges[0].start_page == 1 and ranges[0].end_page == len(reader.pages):
        return [input_pdf]
    return _write_ranges(input_pdf, output_dir, ranges)
