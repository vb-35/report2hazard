from __future__ import annotations

from pathlib import Path

import pytest
from pypdf import PdfReader, PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from multi_hazard_pipeline import pipeline
from multi_hazard_pipeline.errors import PipelineError
from multi_hazard_pipeline.splitter import (
    EventRange,
    construct_ranges,
    extract_outline,
    parse_toc,
    resolve_toc_candidates,
    safe_event_filename,
    split_event_reports,
    validate_ranges,
    validate_separation,
)


class FakeClient:
    def __init__(self, *responses: dict) -> None:
        self.responses = list(responses)
        self.calls = []

    def complete_json(self, **kwargs):
        self.calls.append(kwargs["user_payload"])
        response = self.responses.pop(0)
        kwargs["validate"](response)
        return response


def separation(events, collection_end=None, status="pass", warnings=None):
    return {
        "status": status,
        "events": events,
        "collection_end": collection_end,
        "warnings": warnings or [],
    }


def event(title: str, page: int, quote: str | None = None):
    return {
        "title": title,
        "start_page": page,
        "heading_quote": quote or title,
        "confidence": 0.99,
    }


def make_pdf(path: Path, texts: list[str], *, outline=False) -> Path:
    writer = PdfWriter()
    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        }
    )
    font_ref = writer._add_object(font)
    for text in texts:
        page = writer.add_blank_page(width=300, height=300)
        page[NameObject("/Resources")] = DictionaryObject(
            {
                NameObject("/Font"): DictionaryObject(
                    {NameObject("/F1"): font_ref}
                )
            }
        )
        stream = DecodedStreamObject()
        escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        stream.set_data(f"BT /F1 11 Tf 20 260 Td ({escaped}) Tj ET".encode("latin-1"))
        page[NameObject("/Contents")] = writer._add_object(stream)
    if outline:
        parent = writer.add_outline_item("Reports", 0)
        first = writer.add_outline_item("Event One", 1, parent=parent)
        writer.add_outline_item("Description", 1, parent=first)
        writer.add_outline_item("Event Two", 3, parent=parent)
    with path.open("wb") as handle:
        writer.write(handle)
    return path


def test_outline_traversal_preserves_hierarchy_and_physical_pages(tmp_path: Path) -> None:
    reader = PdfReader(make_pdf(tmp_path / "outline.pdf", ["a", "b", "c", "d"], outline=True))
    outline = extract_outline(reader)
    assert outline[0]["title"] == "Reports"
    assert outline[0]["page"] == 1
    assert outline[0]["children"][0]["title"] == "Event One"
    assert outline[0]["children"][0]["page"] == 2
    assert outline[0]["children"][0]["children"][0]["page"] == 2


def test_toc_occurrence_cannot_be_an_event_start() -> None:
    texts = ["Inhalt\nRiver Event ........ 2", "River Event\nDescription"]
    toc_pages, _ = parse_toc(texts)
    with pytest.raises(ValueError, match="TOC page"):
        validate_separation(separation([event("River Event", 1)]), texts, toc_pages)


def test_printed_toc_page_is_resolved_to_physical_heading() -> None:
    texts = [
        "Inhaltsverzeichnis\nRiver Event ........ 1",
        "front matter",
        "2.1 River Event\nDescription",
    ]
    toc_pages, candidates = parse_toc(texts)
    resolved = resolve_toc_candidates(candidates, texts, toc_pages)
    assert resolved[0]["printed_page"] == 1
    assert resolved[0]["physical_page"] == 3


def test_heading_quote_must_exist_on_claimed_page() -> None:
    with pytest.raises(ValueError, match="heading quote"):
        validate_separation(
            separation([event("River Event", 2, "invented heading")]),
            ["other", "River Event"],
        )


@pytest.mark.parametrize(
    "ranges",
    [
        [EventRange(1, "a", 0, 1)],
        [EventRange(1, "a", 1, 6)],
        [EventRange(1, "a", 3, 2)],
        [EventRange(1, "a", 3, 4), EventRange(2, "b", 2, 2)],
        [EventRange(1, "a", 1, 3), EventRange(2, "b", 3, 4)],
        [EventRange(1, "a", 1, 2), EventRange(2, "a", 1, 2)],
    ],
)
def test_invalid_ranges_are_rejected_before_writing(ranges) -> None:
    with pytest.raises(PipelineError):
        validate_ranges(ranges, 5)


def test_ranges_end_before_next_start_and_collection_end() -> None:
    ranges = construct_ranges(
        [event("Second", 5), event("First", 2)],
        {"page": 8, "heading_quote": "Summary"},
        10,
    )
    assert [(item.start_page, item.end_page) for item in ranges] == [(2, 4), (5, 7)]


def test_stable_files_copy_original_pages_and_exclude_outside_pages(tmp_path: Path) -> None:
    source = make_pdf(
        tmp_path / "collection.pdf",
        ["Front", "Alpha Event", "Alpha body", "Beta/Event", "Summary"],
    )
    result = split_event_reports(
        source,
        tmp_path / "split",
        FakeClient(
            separation(
                [event("Alpha Event", 2), event("Beta/Event", 4)],
                {"page": 5, "heading_quote": "Summary"},
            )
        ),
    )
    assert [path.name for path in result] == [
        "01-alpha-event_pages-002-003.pdf",
        "02-beta-event_pages-004-004.pdf",
    ]
    first, second = map(PdfReader, result)
    assert [len(first.pages), len(second.pages)] == [2, 1]
    assert "Alpha Event" in first.pages[0].extract_text()
    assert "Alpha body" in first.pages[1].extract_text()
    assert "Beta/Event" in second.pages[0].extract_text()
    assert "/Font" in first.pages[0]["/Resources"]
    assert "Front" not in "".join(page.extract_text() for page in first.pages)
    assert "Summary" not in "".join(page.extract_text() for page in second.pages)


def test_filename_is_filesystem_safe() -> None:
    name = safe_event_filename(EventRange(10, "Grünsangerlbach/Feldbach", 147, 153), 176)
    assert name == "10-grunsangerlbach-feldbach_pages-147-153.pdf"


def test_one_event_covering_document_returns_original(tmp_path: Path) -> None:
    source = make_pdf(tmp_path / "single.pdf", ["Only Event", "Body"])
    result = split_event_reports(source, tmp_path / "unused", FakeClient(separation([event("Only Event", 1)])))
    assert result == [source.resolve()]
    assert not (tmp_path / "unused").exists()


def test_collection_runs_each_pdf_separately_and_keeps_siblings_on_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = make_pdf(tmp_path / "first.pdf", ["first"])
    second = make_pdf(tmp_path / "second.pdf", ["second"])
    calls = []

    monkeypatch.setattr(
        "multi_hazard_pipeline.splitter.split_event_reports",
        lambda *args, **kwargs: [first, second],
    )

    def fake_run(input_dir, output_dir, config, *, client, input_paths):
        calls.append(input_paths)
        return {"status": "failed" if input_paths == [second] else "awaiting_human_review"}

    monkeypatch.setattr(pipeline, "run_pipeline", fake_run)
    results = pipeline.run_pdf_collection(
        tmp_path / "collection.pdf", tmp_path / "runs", tmp_path / "split", client=object()
    )
    assert calls == [[first], [second]]
    assert [item["status"] for item in results] == ["awaiting_human_review", "failed"]
    assert first.exists() and second.exists()


REFERENCE_EVENTS = [
    ("Bergsturz Vals", 36, 42),
    ("Schnannerbach", 43, 66),
    ("Gridlontobel und Zeinsbach", 67, 87),
    ("Tauchenbach", 88, 93),
    ("Gemeinden St. Lorenzen am Wechsel und Waldbach-Mönichwald", 94, 105),
    ("Wildbäche Gasen", 106, 121),
    ("Saalach", 122, 130),
    ("Waldbrand und Steinschlag Echernwand", 131, 135),
    ("Hassbach Gemeinde Warth", 136, 146),
    ("Grünsangerlbach/Feldbach", 147, 153),
    ("Erlachgraben", 154, 158),
    ("Schutzwald Lesachtal", 159, 162),
]


def test_ereignisdokumentation_2018_regression(tmp_path: Path) -> None:
    source = Path("results/inputs/FAI/Example_Complete/Ereignisdokumentation2018.pdf")
    if not source.is_file():
        pytest.skip("reference PDF is not available")
    response = separation(
        [event(title, start) for title, start, _ in REFERENCE_EVENTS],
        {"page": 163, "heading_quote": "Zusammenfassung"},
    )
    outputs = split_event_reports(source, tmp_path, FakeClient(response))
    expected_names = [
        safe_event_filename(EventRange(index, title, start, end), 176)
        for index, (title, start, end) in enumerate(REFERENCE_EVENTS, start=1)
    ]
    assert [path.name for path in outputs] == expected_names
    assert [len(PdfReader(path).pages) for path in outputs] == [
        end - start + 1 for _, start, end in REFERENCE_EVENTS
    ]
