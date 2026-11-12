"""Offline checks for complete heading evidence during PDF collection separation."""
import json
import unittest
from dataclasses import replace

from multi_hazard_pipeline import splitter
from multi_hazard_pipeline.config import DEFAULT_CONFIG
from multi_hazard_pipeline.errors import PipelineError


def answer(events=(), boundary=None, status="pass", warnings=()):
    return {"status": status, "events": list(events), "collection_end": boundary,
            "warnings": list(warnings)}


def event(title, page):
    return {"title": title, "start_page": page, "heading_quote": title, "confidence": 0.99}


class ContextClient:
    def __init__(self, events, boundary=None):
        self.events = events
        self.boundary = boundary
        self.payloads = []

    def complete_json(self, **kwargs):
        payload = kwargs["user_payload"]
        self.payloads.append(payload)
        context = dict(payload["page_context"])
        found = [item for item in self.events
                 if item["start_page"] in context and item["heading_quote"] in context[item["start_page"]]]
        boundary = self.boundary if self.boundary and self.boundary["page"] in context else None
        result = answer(found, boundary)
        kwargs["validate"](result)
        return result


class SplitterContextTests(unittest.TestCase):
    def test_toc_leaders_and_author_credits_resolve_to_physical_pages(self):
        texts = [
            "Inhaltsverzeichnis\n4.2 Seigesbach ........ 56\n"
            "4.3 Gallenzerkogelmure ........ 82\n"
            "4.5.3 Die Ereignisse vom 7.6.2015 .... .... 102",
            "IAN Report\nSeite 56\n4.2 Seigesbach (Zusammengestellt aus: JENNER, 2015)",
            "IAN Report\nSeite 82\n4.3 Gallenzerkogelmure (Zusammengestellt aus: AUTHOR, 2015)",
            "Body text\n4.5.3 Die Ereignisse vom 7.6.2015\nHeavy rainfall.",
        ]
        toc_pages, candidates = splitter.parse_toc(texts)
        resolved = splitter.resolve_toc_candidates(candidates, texts, toc_pages)
        self.assertEqual([row["physical_page"] for row in resolved], [2, 3, 4])
        self.assertEqual(resolved[-1]["title"], "4.5.3 Die Ereignisse vom 7.6.2015")

    def test_repeated_subsection_names_keep_their_number(self):
        texts = ["4.1.1 Allgemeine Beschreibung", "4.2.1 Allgemeine Beschreibung"]
        candidate = {"title": texts[1], "printed_page": 56, "toc_page": 0}
        self.assertEqual(splitter.resolve_toc_candidates([candidate], texts, set())[0]["physical_page"], 2)
        self.assertEqual(splitter._heading_key("Die Ereignisse"), "die ereignisse")

    def test_leaf_bookmark_and_heading_below_body_text_get_context(self):
        title = "4.5.4 Das Ereignis vom 3.7.2015"
        texts = ["Collection", "Caption and preceding event.\n" * 65 + title + "\nDebris flow blocked a road."]
        outline = [{"title": "Collection", "page": 1, "children": [
            {"title": title, "page": 2, "children": []}]}]
        payload = splitter._candidate_payload(outline, [], texts)
        self.assertIn(2, payload["candidate_pages"])
        self.assertIn(title, dict(payload["page_context"])[2])
        self.assertIn("Debris flow blocked a road.", dict(payload["page_context"])[2])

    def collection(self):
        titles = [f"4.{index} Catchment {index}" for index in range(1, 19)]
        texts = ["Header\n" + "Earlier body text. " * 100 + "\n" + title + "\nRain triggered a debris flow. " * 30
                 for title in titles]
        texts.append("5 SUMMARY\nNo more event reports.")
        toc = [{"title": title, "printed_page": index + 20, "toc_page": 0, "physical_page": index}
               for index, title in enumerate(titles + ["5 SUMMARY"], 1)]
        return texts, toc, [event(title, page) for page, title in enumerate(titles, 1)]

    def test_batches_preserve_every_heading_and_respect_size_limit(self):
        texts, toc, events = self.collection()
        payload = splitter._candidate_payload([], toc, texts)
        batches = list(splitter._candidate_batches(payload, 5000))
        self.assertGreater(len(batches), 1)
        self.assertTrue(all(len(json.dumps(batch, ensure_ascii=False)) <= 5000 for batch in batches))
        rows = [row for batch in batches for row in batch["page_context"]]
        self.assertEqual(rows, payload["page_context"])
        self.assertEqual(len({row[0] for row in rows}), len(texts))
        for item in events:
            self.assertIn(item["heading_quote"], dict(rows)[item["start_page"]])

    def test_batch_results_merge_into_complete_nonoverlapping_ranges(self):
        texts, toc, events = self.collection()
        boundary = {"page": len(texts), "heading_quote": "5 SUMMARY"}
        client = ContextClient(events, boundary)
        result = splitter._detect_boundaries(client, replace(DEFAULT_CONFIG, batch_max_chars=5000),
                                             [], toc, set(), texts)
        self.assertEqual(result["events"], events)
        self.assertEqual(result["collection_end"], boundary)
        self.assertGreater(len(client.payloads), 1)
        ranges = splitter.construct_ranges(result["events"], result["collection_end"], len(texts))
        self.assertEqual([(row.start_page, row.end_page) for row in ranges],
                         [(page, page) for page in range(1, len(texts))])

    def test_empty_overview_batch_is_allowed(self):
        texts = ["Introduction", "4.1 Creek\nDebris flow"]
        client = ContextClient([event("4.1 Creek", 2)])
        result = splitter._ask(client, {"page_context": [[1, texts[0]]]}, texts, set(), allow_empty=True)
        self.assertEqual(result["events"], [])

    def test_response_cannot_claim_unsupplied_page(self):
        class WrongPageClient:
            def complete_json(self, **kwargs):
                result = answer([event("4.1 Creek", 2)])
                kwargs["validate"](result)
                return result

        with self.assertRaisesRegex(ValueError, "supplied context pages"):
            splitter._ask(WrongPageClient(), {"page_context": [[1, "Introduction"]]},
                          ["Introduction", "4.1 Creek"], set(), allow_empty=True)

    def test_review_required_is_not_overridden_by_other_batches(self):
        texts, toc, events = self.collection()

        class ReviewClient(ContextClient):
            def complete_json(self, **kwargs):
                if self.payloads:
                    return answer(status="review_required", warnings=["Two reports share a page"])
                return super().complete_json(**kwargs)

        result = splitter._detect_boundaries(ReviewClient(events), replace(DEFAULT_CONFIG, batch_max_chars=5000),
                                             [], toc, set(), texts)
        self.assertEqual(result["status"], "review_required")
        self.assertIn("Two reports share a page", result["warnings"])

    def test_context_too_large_fails_without_shrinking_evidence(self):
        with self.assertRaisesRegex(PipelineError, "batch_max_chars"):
            list(splitter._candidate_batches({"outline": [], "page_context": [[1, "Heading" * 100]]}, 100))

    def test_unresolved_structure_falls_back_to_page_scan(self):
        class ScanClient:
            def complete_json(self, **kwargs):
                self.payload = kwargs["user_payload"]
                result = answer([event("4.1 Creek", 1)])
                kwargs["validate"](result)
                return result

        toc = [{"title": "Unreadable heading", "printed_page": 1, "physical_page": None}]
        client = ScanClient()
        result = splitter._detect_boundaries(client, DEFAULT_CONFIG, [], toc, set(), ["4.1 Creek\nFlood"])
        self.assertIn("pages", client.payload)
        self.assertEqual(result["events"][0]["start_page"], 1)

    def test_invalid_source_quote_still_fails(self):
        invalid = event("4.1 Creek", 1) | {"heading_quote": "Invented heading"}
        with self.assertRaisesRegex(ValueError, "heading quote"):
            splitter.validate_separation(answer([invalid]), ["4.1 Creek\nFlood"], set())


if __name__ == "__main__":
    unittest.main()
