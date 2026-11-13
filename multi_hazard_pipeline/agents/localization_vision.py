"""Report figures for vision-assisted localization, with file/page provenance.

Vision mode sends the extracted report text together with the figures and photographs embedded in the report (maps,
profiles, photos), never pictures of whole pages: the text already travels as text. Each figure keeps its file, page,
printed page and the captions printed on that page. Figures drawn as vector graphics have no embedded picture and are
listed as such, not rendered.
"""
from __future__ import annotations

import base64
import hashlib
from collections import Counter
from io import BytesIO
from pathlib import Path

from ..core import resolve_path, write_json

MAX_PER_CALL = 8
MAX_EDGE_PX = 1568  # larger figures are downscaled; the model would not see more detail
MIN_EDGE_PX = 100   # icons, rules and thin strips are not figures
MIN_LONG_EDGE_PX = 200
REPEATED_ON_PAGES = 3  # the same picture on several pages is a logo or header, not a figure
PICTURE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp"}


def _jpeg(pil) -> bytes:
    from PIL import Image

    image = pil.convert("RGB") if pil.mode != "RGB" else pil
    if max(image.size) > MAX_EDGE_PX:
        image = image.copy()
        image.thumbnail((MAX_EDGE_PX, MAX_EDGE_PX), Image.LANCZOS)
    out = BytesIO()
    image.save(out, "JPEG", quality=85)
    return out.getvalue()


class ReportImages:
    def __init__(self, manifest, pages, folder: Path, log, captions=None):
        self.folder, self.log = folder, log
        self.sources, self.errors, self.cache = {}, [], {}
        self.used = set()
        self.vector_figures = []  # captions on pages without an embedded picture
        captions_by_page: dict = {}
        for c in captions or []:
            captions_by_page.setdefault(c.get("pdf_page"), []).append(c)
        paths = list(dict.fromkeys(resolve_path(i["path"]) for i in manifest.get("inputs", []) if i.get("path")))
        names = Counter(p.name.casefold() for p in paths)
        for path in paths:
            suffix = path.suffix.lower()
            if suffix != ".pdf" and suffix not in PICTURE_SUFFIXES:
                continue
            try:
                if names[path.name.casefold()] != 1:
                    raise ValueError("ambiguous duplicate filename in report inputs")
                if not path.is_file():
                    raise ValueError("source file is missing")
                if suffix == ".pdf":
                    self._add_pdf(path, pages, captions_by_page)
                else:
                    from PIL import Image

                    with Image.open(path) as picture:
                        self._add(path, None, {}, picture, [], index=1)
            except Exception as exc:
                self._error(f"{path.name}: {exc}")

    def _add_pdf(self, path: Path, pages, captions_by_page):
        from pypdf import PdfReader

        reader = PdfReader(str(path))
        found = []  # (file_page, index, digest, pil)
        for number, page in enumerate(reader.pages, start=1):
            try:
                embedded = list(page.images)
            except Exception as exc:  # a broken image stream must not stop the run
                self._error(f"{path.name} page {number}: figure extraction failed ({exc})")
                continue
            for index, image in enumerate(embedded, start=1):
                try:
                    pil = image.image
                except Exception as exc:
                    self._error(f"{path.name} page {number} figure {index}: unreadable ({exc})")
                    continue
                if pil is None or min(pil.size) < MIN_EDGE_PX or max(pil.size) < MIN_LONG_EDGE_PX:
                    continue
                found.append((number, index, hashlib.sha1(image.data).hexdigest(), pil))
        pages_of = Counter(digest for _, digest in {(n, d) for n, _, d, _ in found})
        with_figure = set()
        for number, index, digest, pil in found:
            if pages_of[digest] >= REPEATED_ON_PAGES:
                continue
            matches = [p for p in pages if Path(p.get("file", "")).name.casefold() == path.name.casefold()
                       and p.get("file_page") == number]
            p = matches[0] if len(matches) == 1 else {}
            self._add(path, number, p, pil, captions_by_page.get(p.get("pdf_page"), []), index)
            with_figure.add(number)
        for p in pages:
            if Path(p.get("file", "")).name.casefold() == path.name.casefold() and p.get("file_page") not in with_figure:
                for caption in captions_by_page.get(p.get("pdf_page"), []):
                    if str(caption.get("kind", "")).casefold().startswith(("tab", "tav")):
                        continue  # tables travel as text
                    self.vector_figures.append(dict(filename=path.name, page=p.get("pdf_page"),
                                                    printed_page=p.get("printed_page"),
                                                    caption=str(caption.get("caption", ""))[:200]))

    def _add(self, path: Path, number, page: dict, pil, captions, index):
        source_id = f"image-{len(self.sources) + 1:04d}"
        self.folder.mkdir(parents=True, exist_ok=True)
        target = self.folder / f"{source_id}.jpg"
        data = _jpeg(pil)
        target.write_bytes(data)
        from PIL import Image

        with Image.open(BytesIO(data)) as saved:
            width, height = saved.size
        self.sources[source_id] = dict(
            source_id=source_id, kind="report figure", path=str(path), filename=path.name, file_page=number,
            figure_on_page=index, page=page.get("pdf_page"), printed_page=page.get("printed_page"),
            captions_on_page=[str(c.get("caption", ""))[:300] for c in captions], width=width, height=height,
            render=f"llm/images/{target.name}")

    def _error(self, message):
        self.errors.append(message)
        self.log(f"  vision: {message}; continuing with available evidence")

    def select(self, page_numbers):
        """Figures on the evidence pages, then figures on their neighboring file pages (a caption or reference may sit
        next to its figure), then figures given as separate picture files. No figures: the call is text-only."""
        primary = [s for page in page_numbers for s in self.sources.values() if s["page"] == page]
        evidence_pages = {(s["path"], s["file_page"]) for s in primary}
        nearby = [s for s in self.sources.values() if s["file_page"] is not None and any(
            s["path"] == path and abs(s["file_page"] - n) == 1 for path, n in evidence_pages if n is not None)]
        pictures = [s for s in self.sources.values() if s["file_page"] is None]
        return list(dict.fromkeys(s["source_id"] for s in primary + nearby + pictures))

    def attach(self, source_ids):
        images, sources = [], []
        for sid in source_ids:
            if len(images) == MAX_PER_CALL:
                break
            s = self.sources.get(sid)
            if s is None:
                continue
            target = self.folder / f"{sid}.jpg"
            try:
                data = target.read_bytes()
            except OSError as exc:
                self._error(f"{s['filename']} figure {sid}: file missing ({exc})")
                continue
            images.append("data:image/jpeg;base64," + base64.b64encode(data).decode("ascii"))
            sources.append(dict({k: v for k, v in s.items() if k != "path"}, image_number=len(images)))
            self.used.add(sid)
        write_json(self.folder.parent / "image_inventory.json", self.summary())
        return images, sources

    def summary(self):
        return {"sources": list(self.sources.values()), "inspected": sorted(self.used),
                "uninspected": sorted(set(self.sources) - self.used), "errors": self.errors,
                "vector_figures_not_attached": self.vector_figures}
