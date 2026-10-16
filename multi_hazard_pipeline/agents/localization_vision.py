"""Bounded report-page attachments with file/page provenance for localization."""
from __future__ import annotations

import base64
from collections import Counter
from pathlib import Path

from ..core import resolve_path, write_json
from ..vision import load_images


class ReportImages:
    def __init__(self, manifest, pages, folder: Path, log):
        import pypdfium2 as pdfium

        self.folder, self.log = folder, log
        self.sources, self.errors, self.cache = {}, [], {}
        self.used = set()
        paths = list(dict.fromkeys(resolve_path(i["path"]) for i in manifest.get("inputs", []) if i.get("path")))
        names = Counter(p.name.casefold() for p in paths)
        for path in paths:
            if path.suffix.lower() not in {".pdf", ".png", ".jpg", ".jpeg", ".webp"}:
                continue
            try:
                if names[path.name.casefold()] != 1:
                    raise ValueError("ambiguous duplicate filename in report inputs")
                if not path.is_file():
                    raise ValueError("source file is missing")
                if path.suffix.lower() == ".pdf":
                    with pdfium.PdfDocument(str(path)) as doc:
                        numbers = range(1, len(doc) + 1)
                else:
                    numbers = [None]
                for number in numbers:
                    matches = [p for p in pages if Path(p.get("file", "")).name.casefold() == path.name.casefold()
                               and (number is None or p.get("file_page") == number)]
                    p = matches[0] if len(matches) == 1 else {}
                    source_id = f"image-{len(self.sources) + 1:04d}"
                    self.sources[source_id] = dict(source_id=source_id, path=str(path), filename=path.name,
                                                   file_page=number, page=p.get("pdf_page"),
                                                   printed_page=p.get("printed_page"))
            except Exception as exc:
                self._error(f"{path.name}: {exc}")

    def _error(self, message):
        self.errors.append(message)
        self.log(f"  vision: {message}; continuing with available evidence")

    def select(self, page_numbers):
        """Evidence first, then neighboring file pages and remaining report pages."""
        primary = [s for page in page_numbers for s in self.sources.values() if s["page"] == page]
        nearby = [s for p in primary for s in self.sources.values()
                  if s["path"] == p["path"] and s["file_page"] is not None and p["file_page"] is not None
                  and abs(s["file_page"] - p["file_page"]) == 1]
        return list(dict.fromkeys(s["source_id"] for s in primary + nearby + list(self.sources.values())))

    def attach(self, source_ids):
        images, sources = [], []
        for sid in source_ids:
            if len(images) == 8:
                break
            s = self.sources[sid]
            if sid not in self.cache:
                try:
                    data, meta = load_images(Path(s["path"]), [s["file_page"]] if s["file_page"] is not None else None)
                    self.folder.mkdir(parents=True, exist_ok=True)
                    target = self.folder / f"{sid}.jpg"
                    target.write_bytes(base64.b64decode(data[0].split(",", 1)[1]))
                    self.cache[sid] = dict(s, width=meta[0]["width"], height=meta[0]["height"],
                                           render=f"llm/images/{target.name}")
                except Exception as exc:
                    self.cache[sid] = None
                    self._error(f"{s['filename']} page {s['file_page']}: render failed ({exc})")
            cached = self.cache[sid]
            if cached is None:
                continue
            images.append("data:image/jpeg;base64," + base64.b64encode((self.folder / f"{sid}.jpg").read_bytes()).decode("ascii"))
            sources.append(dict(cached, image_number=len(images)))
            self.used.add(sid)
        write_json(self.folder.parent / "image_inventory.json", self.summary())
        return images, sources

    def summary(self):
        return {"sources": list(self.sources.values()), "inspected": sorted(self.used),
                "uninspected": sorted(set(self.sources) - self.used), "errors": self.errors}
