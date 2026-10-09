from __future__ import annotations

import csv
import json
import os
import re
import tempfile
import threading
from contextlib import contextmanager
from json import JSONDecodeError
from pathlib import Path
from typing import Any


_RUN_LOCKS: dict[str, threading.RLock] = {}
_RUN_LOCKS_GUARD = threading.Lock()


def resolve_path(path: str | Path) -> Path:
    """Resolve filesystem paths, bypassing Windows MAX_PATH without registry changes."""
    resolved = Path(path).resolve()
    if os.name != "nt":
        return resolved
    value = str(resolved)
    if value.startswith("\\\\?\\"):
        return resolved
    if value.startswith("\\\\"):
        return Path("\\\\?\\UNC\\" + value[2:])
    return Path("\\\\?\\" + value)


@contextmanager
def run_lock(path: Path):
    """Serialize state transitions for one run within the local process."""
    key = str(resolve_path(path))
    with _RUN_LOCKS_GUARD:
        lock = _RUN_LOCKS.setdefault(key, threading.RLock())
    with lock:
        yield


def normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", value.replace("\x00", " ")).strip()


DOUBLE_QUOTE_MARKS = '"\u00ab\u00bb\u201c\u201d\u201e\u201f\u275d\u275e\u276e\u276f\u301d\u301e\u301f\uff02'
_REMOVE_DOUBLE_QUOTES = str.maketrans("", "", DOUBLE_QUOTE_MARKS)
_INCH_MARK = re.compile(r'(?<![\w.,' + re.escape(DOUBLE_QUOTE_MARKS) + r'])(\d+(?:[.,]\d+)?)"(?!\w)')


def _preserve_inch_marks(value: str) -> str:
    # A numeric measurement delimiter must retain its unit in the model's view.
    return _INCH_MARK.sub(r'\1' + '\u2033', value)


def model_text(value: str) -> str:
    """Remove quotation delimiters that can derail structured decoding; keep apostrophes and primes."""
    return _preserve_inch_marks(value).translate(_REMOVE_DOUBLE_QUOTES)


def restore_source_quote(text: str, quote: str) -> str | None:
    """Resolve a model-facing quote to an original substring, rejecting ambiguous restorations."""
    text, quote = normalize_text(text), normalize_text(quote)
    if quote and quote.casefold() in text.casefold():
        return quote
    needle = normalize_text(model_text(quote)).casefold()
    if not needle:
        return None
    characters, offsets = [], []
    for index, character in enumerate(_preserve_inch_marks(text)):
        if character in DOUBLE_QUOTE_MARKS:
            continue
        if character == " " and (not characters or characters[-1] == " "):
            continue
        for folded in character.casefold():
            characters.append(folded)
            offsets.append(index)
    cleaned = "".join(characters)
    matches: set[str] = set()
    start = cleaned.find(needle)
    while start >= 0:
        left, right = offsets[start], offsets[start + len(needle) - 1] + 1
        while left > 0 and text[left - 1] in DOUBLE_QUOTE_MARKS:
            left -= 1
        while right < len(text) and text[right] in DOUBLE_QUOTE_MARKS:
            right += 1
        matches.add(text[left:right])
        if len(matches) > 1:
            return None
        start = cleaned.find(needle, start + 1)
    return next(iter(matches), None)


def canonicalize_controlled_value(value: Any, allowed: tuple[str, ...], aliases: dict[str, str]) -> Any:
    if not isinstance(value, str):
        return value
    text = normalize_text(value)
    if text in allowed:
        return text
    return aliases.get(text.casefold(), text)


def parse_json_object(raw: str) -> Any:
    raw = raw.strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.DOTALL)
    try:
        return json.loads(raw)
    except JSONDecodeError:
        sanitized = re.sub(r'\\u(?![0-9a-fA-F]{4})', r"\\\\u", raw)
        sanitized = re.sub(r'\\(?![\"\\/bfnrtu])', r"\\\\", sanitized)
        try:
            return json.loads(sanitized)
        except JSONDecodeError as exc:
            if exc.msg == "Invalid control character at":
                return json.JSONDecoder(strict=False).raw_decode(sanitized)[0]
            if exc.msg != "Extra data":
                raise
            return json.JSONDecoder().raw_decode(sanitized)[0]


def write_json(path: Path, payload: Any) -> None:
    path = resolve_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as temporary:
            json.dump(payload, temporary, indent=2, ensure_ascii=True)
        os.replace(temporary_name, path)
    except Exception:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


def read_json(path: Path) -> Any:
    return json.loads(resolve_path(path).read_text(encoding="utf-8"))


def write_csv(path: Path, rows: list[dict[str, Any]], columns: tuple[str, ...]) -> None:
    path = resolve_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(handle, "w", newline="", encoding="utf-8") as temporary:
            writer = csv.DictWriter(temporary, fieldnames=list(columns))
            writer.writeheader()
            for row in rows:
                writer.writerow({key: row[key] for key in columns})
        os.replace(temporary_name, path)
    except Exception:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise
