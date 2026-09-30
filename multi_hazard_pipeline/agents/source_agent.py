from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any

import pdfplumber
from docx import Document
from pypdf import PdfReader

from ..core import normalize_text
from ..errors import PipelineError


def stable_document_id(report_id: str, path: Path) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", path.stem.casefold()).strip("-") or "document"
    digest = hashlib.sha256(path.name.casefold().encode("utf-8")).hexdigest()[:8]
    return f"{report_id}-document-{slug}-{digest}"


def discover_inputs(input_dir: Path) -> list[Path]:
    if not input_dir.exists():
        raise PipelineError(f"input directory does not exist: {input_dir}")
    if not input_dir.is_dir():
        raise PipelineError(f"input path is not a directory: {input_dir}")
    files = [
        path
        for path in sorted(input_dir.iterdir())
        if path.is_file() and path.suffix.lower() in {".docx", ".pdf", ".txt"}
    ]
    if not files:
        raise PipelineError(f"no supported inputs found in {input_dir}")
    return files


def extract_docx(path: Path, doc_id: str, next_id: int) -> tuple[list[dict[str, Any]], int]:
    document = Document(path)
    chunks: list[dict[str, Any]] = []
    for index, paragraph in enumerate(document.paragraphs, start=1):
        text = normalize_text(paragraph.text)
        if not text:
            continue
        chunks.append(
            {
                "chunk_id": f"{doc_id}-paragraph-{index:04d}",
                "doc_id": doc_id,
                "document_id": doc_id,
                "file": path.name,
                "filename": path.name,
                "source_kind": "docx",
                "source_type": "docx",
                "paragraph": index,
                "text": text,
            }
        )
        next_id += 1
    return chunks, next_id


def extract_pdf(path: Path, doc_id: str, next_id: int) -> tuple[list[dict[str, Any]], int]:
    chunks: list[dict[str, Any]] = []
    fallback_reader = PdfReader(str(path))
    with pdfplumber.open(path) as pdf:
        for page_index, page in enumerate(pdf.pages, start=1):
            text = normalize_text(page.extract_text() or "")
            if not text:
                text = normalize_text(fallback_reader.pages[page_index - 1].extract_text() or "")
            if not text:
                continue
            chunks.append(
                {
                    "chunk_id": f"{doc_id}-page-{page_index:04d}",
                    "doc_id": doc_id,
                    "document_id": doc_id,
                    "file": path.name,
                    "filename": path.name,
                    "source_kind": "pdf",
                    "source_type": "pdf",
                    "page": page_index,
                    "text": text,
                }
            )
            next_id += 1
    return chunks, next_id


def extract_txt(path: Path, doc_id: str, next_id: int) -> tuple[list[dict[str, Any]], int]:
    text = normalize_text(path.read_text(encoding="utf-8"))
    if not text:
        return [], next_id
    return (
        [
            {
                "chunk_id": f"{doc_id}-text-0001",
                "doc_id": doc_id,
                "document_id": doc_id,
                "file": path.name,
                "filename": path.name,
                "source_kind": "txt",
                "source_type": "txt",
                "text": text,
            }
        ],
        next_id + 1,
    )


def source_agent(input_paths: list[Path], doc_id: str) -> dict[str, Any]:
    if not input_paths:
        raise PipelineError("source extraction requires at least one report file")
    paths = [Path(path).resolve() for path in input_paths]
    parents = {path.parent for path in paths}
    if len(parents) != 1:
        raise PipelineError("files for one report must be selected from one input directory")
    for path in paths:
        if not path.is_file():
            raise PipelineError(f"report file does not exist: {path}")
        if path.suffix.lower() not in {".docx", ".pdf", ".txt"}:
            raise PipelineError(f"unsupported report file: {path.name}")
    chunks: list[dict[str, Any]] = []
    next_id = 1
    documents: list[dict[str, str]] = []
    for path in sorted(paths):
        document_id = stable_document_id(doc_id, path)
        documents.append({"document_id": document_id, "filename": path.name, "source_type": path.suffix.lower()[1:]})
        suffix = path.suffix.lower()
        if suffix == ".docx":
            new_chunks, next_id = extract_docx(path, document_id, next_id)
        elif suffix == ".pdf":
            new_chunks, next_id = extract_pdf(path, document_id, next_id)
        elif suffix == ".txt":
            new_chunks, next_id = extract_txt(path, document_id, next_id)
        else:
            continue
        chunks.extend(new_chunks)
    if not chunks:
        raise PipelineError("source extraction produced no usable chunks")
    return {
        "doc_id": doc_id,
        "report_id": doc_id,
        "status": "pass",
        "files": [path.name for path in sorted(paths)],
        "documents": documents,
        "file_association": (
            "The selected input directory is one report boundary; each supported file directly in it is an explicitly "
            "selected part of that report and receives its own document_id. Directories are not recursively combined."
        ),
        "chunk_count": len(chunks),
        "chunks": chunks,
    }
