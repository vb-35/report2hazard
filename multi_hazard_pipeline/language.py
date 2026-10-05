"""Offline language spans and alphabetic-weighted decisions for one logical report."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from functools import lru_cache
from hashlib import sha256
from importlib.metadata import version
import json
from typing import Any

from lingua import Language, LanguageDetectorBuilder

from .config import DEFAULT_CONFIG, PipelineConfig
from .errors import PipelineError
from .schemas import SUPPORTED_SOURCE_LANGUAGES


LANGUAGE_SETTINGS = ("mainly_english_threshold", "language_min_confidence", "language_min_margin")
ANALYSIS_VERSION = 1


class LanguageDetectionError(PipelineError):
    def __init__(self, message: str, analysis: dict[str, Any]):
        self.language_analysis = analysis
        super().__init__(message)


def language_settings(config: PipelineConfig) -> dict[str, float]:
    return {name: getattr(config, name) for name in LANGUAGE_SETTINGS}


def recorded_language_config(config: PipelineConfig, manifest: dict[str, Any]) -> PipelineConfig:
    saved = manifest.get("configuration", {})
    return replace(config, **{name: saved[name] for name in LANGUAGE_SETTINGS if name in saved})


@lru_cache(maxsize=1)
def detectors():
    languages = [Language.from_str(name) for name in SUPPORTED_SOURCE_LANGUAGES]
    return (
        LanguageDetectorBuilder.from_languages(*languages).build(),
        LanguageDetectorBuilder.from_languages(Language.ENGLISH).build(),
    )


def alphabetic_count(text: str) -> int:
    return sum(character.isalpha() for character in text)


def majority_decision(total: int, english: int, unresolved: int, threshold: float) -> dict[str, Any]:
    if total == 0:
        return {"english_share_lower": None, "english_share_upper": None, "decision": "no_readable_text"}
    lower, upper = english / total, (english + unresolved) / total
    decision = "skip_translation" if lower > threshold else "translate" if upper <= threshold else "unresolved"
    return {"english_share_lower": lower, "english_share_upper": upper, "decision": decision}


def _confidence(text: str, config: PipelineConfig) -> dict[str, Any]:
    detector, english_detector = detectors()
    scores = detector.compute_language_confidence_values(text)
    top = scores[0] if scores else None
    score = top.value if top else 0.0
    margin = score - (scores[1].value if len(scores) > 1 else 0.0)
    language = top.language.name.title() if top else "Unknown"
    english_conflict = language == "English" and english_detector.detect_language_of(text) != Language.ENGLISH
    if score < config.language_min_confidence or margin < config.language_min_margin or english_conflict:
        language = "Unknown"
    return {"language": language, "confidence": score, "margin": margin, "english_conflict": english_conflict}


def detect_spans(text: str, config: PipelineConfig) -> list[dict[str, Any]]:
    """Cover every character once; context never replaces a confident language switch."""
    detector, _ = detectors()
    spans = []
    cursor = 0
    for result in detector.detect_multiple_languages_of(text):
        start, end = result.start_index, result.end_index
        if not cursor <= start < end <= len(text):
            raise PipelineError("local language detector returned overlapping or invalid span offsets")
        if cursor < start:
            spans.append({"start": cursor, "end": start, **_confidence(text[cursor:start], config)})
        confidence = _confidence(text[start:end], config)
        if confidence["language"] != result.language.name.title():
            confidence["language"] = "Unknown"
        spans.append({"start": start, "end": end, **confidence})
        cursor = end
    if cursor < len(text):
        spans.append({"start": cursor, "end": len(text), **_confidence(text[cursor:], config)})

    # ponytail: local 200-character context is a heuristic; calibrate against more reports before broadening languages.
    initial = deepcopy(spans)
    for index, span in enumerate(spans):
        count = alphabetic_count(text[span["start"]:span["end"]])
        if not count or (span["language"] != "Unknown" and count >= 100):
            continue
        if span["english_conflict"]:
            continue
        left = next((item for item in reversed(initial[:index]) if item["language"] != "Unknown"), None)
        right = next((item for item in initial[index + 1:] if item["language"] != "Unknown"), None)
        if left and right and left["language"] != right["language"]:
            continue
        context_start = max(0, span["start"] - 200)
        context_end = min(len(text), span["end"] + 200)
        neighbors = {item["language"] for item in (left, right) if item}
        if neighbors:
            neighbor_language = next(iter(neighbors))
            for other in initial:
                if other["language"] in {"Unknown", neighbor_language}:
                    continue
                if other["end"] <= span["start"]:
                    context_start = max(context_start, other["end"])
                elif other["start"] >= span["end"]:
                    context_end = min(context_end, other["start"])
        context = _confidence(text[context_start:context_end], config)
        span["context_language"] = context["language"]
        if (span["language"] == "Unknown" and context["language"] != "Unknown"
                and (not neighbors or context["language"] in neighbors)):
            span.update(context)
            span["context_resolved"] = True
    return spans


def _original_chunks(chunks: list[dict[str, Any]]):
    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for chunk in chunks:
        key = (chunk.get("document_id", chunk.get("filename", "")), chunk.get("parent_chunk_id", chunk["chunk_id"]))
        groups.setdefault(key, []).append(chunk)
    for parts in groups.values():
        if "parent_chunk_id" not in parts[0]:
            yield parts[0]["text"], [(parts[0], 0, len(parts[0]["text"]))]
            continue
        ordered = sorted(parts, key=lambda item: item["char_start"])
        cursor = 0
        locations = []
        for part in ordered:
            start, end = part["char_start"], part["char_end"]
            if start != cursor or end - start != len(part["text"]):
                raise PipelineError("split source chunks have missing, overlapping, or inconsistent offsets")
            locations.append((part, start, end))
            cursor = end
        yield "".join(part["text"] for part in ordered), locations


def analyze_language(
    source: dict[str, Any], config: PipelineConfig = DEFAULT_CONFIG,
    previous: dict[str, Any] | None = None,
) -> dict[str, Any]:
    fingerprint = sha256(json.dumps(source, sort_keys=True, ensure_ascii=True).encode()).hexdigest()
    identity = {
        "source_fingerprint": fingerprint,
        "detector_version": version("lingua-language-detector"),
        "analysis_version": ANALYSIS_VERSION,
        "settings": language_settings(config),
    }
    if previous and all(previous.get(key) == value for key, value in identity.items()):
        analysis = deepcopy(previous)
        for detail in analysis["chunks"].values():
            detail["translation_applied"] = False
        analysis["translation_request_count"] = 0
    else:
        analysis = identity | {
            "counts": dict.fromkeys(SUPPORTED_SOURCE_LANGUAGES, 0),
            "unresolved_count": 0, "total_alphabetic_count": 0,
            "files": {}, "chunks": {}, "translation_request_count": 0,
        }
        for text, locations in _original_chunks(source["chunks"]):
            spans = detect_spans(text, config)
            for chunk, start, end in locations:
                counts = dict.fromkeys(SUPPORTED_SOURCE_LANGUAGES, 0)
                unresolved = 0
                chunk_spans = []
                for span in spans:
                    a, b = max(start, span["start"]), min(end, span["end"])
                    if a >= b:
                        continue
                    count = alphabetic_count(text[a:b])
                    label = span["language"]
                    if label == "Unknown":
                        unresolved += count
                    else:
                        counts[label] += count
                    chunk_spans.append(span | {"start": a - start, "end": b - start, "alphabetic_count": count})
                languages = [name for name, count in counts.items() if count]
                label = "Unknown" if unresolved or not languages else languages[0] if len(languages) == 1 else "Mixed"
                total = alphabetic_count(chunk["text"])
                if sum(counts.values()) + unresolved != total:
                    raise PipelineError("local language analysis did not account for every alphabetic character")
                analysis["chunks"][chunk["chunk_id"]] = {
                    "languages": languages, "source_language": label,
                    "counts": counts, "unresolved_count": unresolved,
                    "total_alphabetic_count": total, "spans": chunk_spans,
                    "translation_applied": False,
                }
                file_id = chunk.get("document_id", chunk.get("filename", source["doc_id"]))
                file = analysis["files"].setdefault(file_id, {
                    "filename": chunk.get("filename", file_id),
                    "counts": dict.fromkeys(SUPPORTED_SOURCE_LANGUAGES, 0),
                    "unresolved_count": 0, "total_alphabetic_count": 0,
                })
                for aggregate in (analysis, file):
                    for name, count in counts.items():
                        aggregate["counts"][name] += count
                    aggregate["unresolved_count"] += unresolved
                    aggregate["total_alphabetic_count"] += total
        analysis.update(majority_decision(
            analysis["total_alphabetic_count"], analysis["counts"]["English"],
            analysis["unresolved_count"], config.mainly_english_threshold,
        ))
    if analysis["decision"] in {"unresolved", "no_readable_text"}:
        message = (
            "No readable language: source contains no alphabetic text. Check extraction or supply readable text."
            if analysis["decision"] == "no_readable_text" else
            "Local language detection cannot decide whether this report is mainly English "
            f"(English share {analysis['english_share_lower']:.1%}–{analysis['english_share_upper']:.1%}, "
            f"{analysis['unresolved_count']} unresolved alphabetic characters). "
            "Check ambiguous/OCR text and the four supported languages, then rerun."
        )
        raise LanguageDetectionError(message, analysis)
    return analysis


def language_summary(analysis: dict[str, Any]) -> dict[str, Any]:
    keys = ("detector_version", "settings", "counts", "unresolved_count", "total_alphabetic_count",
            "english_share_lower", "english_share_upper", "decision", "translation_request_count")
    summary = {key: analysis[key] for key in keys}
    summary["languages"] = [name for name, count in analysis["counts"].items() if count]
    summary["reason"] = {
        "skip_translation": "Mainly English; minority-language passages retained for direct interpretation.",
        "translate": "Report is not mainly English; English-only chunks pass through locally.",
        "unresolved": "Uncertainty crosses the mainly-English threshold.",
        "no_readable_text": "No readable alphabetic text.",
    }[analysis["decision"]]
    return summary


def language_prompt_context(source: dict[str, Any]) -> dict[str, Any]:
    analysis = source.get("language_analysis")
    if not analysis:
        return {"decision": "legacy_english_translation", "chunks": {}}
    return {
        "decision": analysis["decision"],
        "chunks": {chunk_id: {"translation_applied": detail["translation_applied"]}
                   for chunk_id, detail in analysis["chunks"].items()},
    }
