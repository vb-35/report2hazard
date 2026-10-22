"""Report inspection: text, tables, page renders, embedded figures, captions, quotes and clues."""
from __future__ import annotations

import collections
import csv
import json
import re
from pathlib import Path

from .runctx import DEFAULT_CAPTION_PREFIXES, REPORT_TEXT, Run, read_json, write_json

# ---------------------------------------------------------------------------- extraction


def _detect_offset(texts: list[str]) -> tuple[int, dict]:
    """Printed page = PDF page + offset, from footer/header page numbers."""
    votes = collections.Counter()
    for i, t in enumerate(texts):
        lines = [ln.strip() for ln in t.splitlines() if ln.strip()]
        cand = lines[:3] + lines[-3:]
        for ln in cand:
            m = re.fullmatch(r"(?:Seite|Page|S\.|p\.|Pag\.|Pagina)?\s*(\d{1,4})(?:\s*(?:/|von|of)\s*\d+)?", ln, re.I)
            if m:
                votes[int(m.group(1)) - (i + 1)] += 1
    if not votes:
        return 0, {}
    off, n = votes.most_common(1)[0]
    return (off if n >= max(2, 0.3 * len(texts)) else 0), dict(votes.most_common(5))


def _captions(run: Run, texts: list[str]) -> list[dict]:
    prefixes = run.config.get("caption_prefixes") or DEFAULT_CAPTION_PREFIXES
    pat = re.compile(r"^\s*(" + "|".join(re.escape(p) for p in prefixes) + r")\s*(\d+[a-z]?)\s*[:.\-–]\s*(.*)$", re.I)
    out = []
    for i, t in enumerate(texts):
        lines = t.splitlines()
        for j, ln in enumerate(lines):
            m = pat.match(ln)
            if not m:
                continue
            parts = [ln.strip()]
            for nxt in lines[j + 1 : j + 5]:
                s = nxt.strip()
                if not s or pat.match(nxt) or re.fullmatch(r"(Seite|Page)?\s*\d+", s, re.I):
                    break
                joined = " ".join(parts)
                if not (parts[-1].endswith("-") or s[:1].islower() or s[:1] in "(–-" or joined.count("(") > joined.count(")")
                        or joined.count("„") > joined.count("“")):
                    break
                parts.append(s)
            caption = re.sub(r"-\s+(?=[a-zäöüß])", "", " ".join(parts))
            out.append(dict(kind=m.group(1), number=m.group(2), pdf_page=i + 1, printed_page=run.printed(i + 1), caption=caption))
    return out


def extract(run: Run, scale: float = 2.0, contact: bool = True, min_image_px: int = 150) -> dict:
    """Extract text (pypdf), tables (pdfplumber), page renders (pypdfium2), embedded images and captions."""
    import pdfplumber
    import pypdfium2 as pdfium
    from PIL import Image, ImageDraw
    from pypdf import PdfReader

    pdf_path = run.report_path
    if not pdf_path.exists():
        raise SystemExit(f"Report not found: {pdf_path}")
    run.ensure_dirs()
    reader = PdfReader(str(pdf_path))
    texts = [(p.extract_text() or "") for p in reader.pages]
    if run.config.get("printed_page_offset") is None:
        off, votes = _detect_offset(texts)
        run.config["printed_page_offset"] = off
        run.config["printed_page_offset_votes"] = votes
        run.save_config()

    pages = [dict(pdf_page=i + 1, printed_page=run.printed(i + 1), text=t) for i, t in enumerate(texts)]
    write_json(run.dir / REPORT_TEXT, pages)

    tables = []
    with pdfplumber.open(str(pdf_path)) as pdf:
        for i, p in enumerate(pdf.pages):
            for k, tb in enumerate(p.extract_tables() or []):
                if tb and len(tb) > 1:
                    tables.append(dict(pdf_page=i + 1, printed_page=run.printed(i + 1), index=k, rows=tb))
    write_json(run.raw / "report_tables.json", tables)

    images = []
    for i, p in enumerate(reader.pages):
        try:
            imgs = list(p.images)
        except Exception as exc:  # broken image streams should not stop inspection
            print(f"page {i + 1}: image extraction failed ({exc})")
            continue
        for k, im in enumerate(imgs):
            try:
                pil = im.image
            except Exception:
                continue
            if pil is None or max(pil.size) < min_image_px:
                continue
            ext = ".jpg" if im.name.lower().endswith((".jpg", ".jpeg")) else ".png"
            name = f"p{i + 1:02d}_img{k + 1}{ext}"
            target = run.inspect / name
            if ext == ".jpg":
                target.write_bytes(im.data)
            else:
                pil.convert("RGB").save(target)
            images.append(dict(file=f"tmp/report_inspection/{name}", pdf_page=i + 1, printed_page=run.printed(i + 1),
                               width=pil.size[0], height=pil.size[1], pdf_name=im.name))
    write_json(run.raw / "report_images.json", images)

    caps = _captions(run, texts)
    write_json(run.raw / "report_captions.json", caps)

    doc = pdfium.PdfDocument(str(pdf_path))
    thumbs = []
    for i in range(len(doc)):
        im = doc[i].render(scale=scale).to_pil()
        im.save(run.inspect / f"page_{i + 1:02d}.png")
        if contact:
            th = im.copy()
            th.thumbnail((330, 465))
            card = Image.new("RGB", (350, 495), "white")
            card.paste(th, (10, 25))
            ImageDraw.Draw(card).text((10, 5), f"PDF {i + 1} / printed {run.printed(i + 1)}", fill="black")
            thumbs.append(card)
    for start in range(0, len(thumbs), 9):
        sheet = Image.new("RGB", (1050, 1485), "#cccccc")
        for k, card in enumerate(thumbs[start : start + 9]):
            sheet.paste(card, ((k % 3) * 350, (k // 3) * 495))
        sheet.save(run.inspect / f"contact_{start // 9 + 1}.png")
    return dict(pages=len(pages), offset=run.page_offset, images=len(images), tables=len(tables), captions=len(caps))


# ---------------------------------------------------------------------------- inspection renders


def _parse_box(s: str | None):
    if not s:
        return None
    v = [float(x) for x in re.split(r"[ ,;]+", s.strip()) if x]
    if len(v) != 4:
        raise SystemExit("--crop needs x0,y0,x1,y1")
    return v


def _parse_points(s: str | None):
    if not s:
        return []
    pts = []
    for chunk in s.split(";"):
        if chunk.strip():
            x, y = [float(v) for v in chunk.split(",")]
            pts.append((x, y))
    return pts


def render(run: Run, page: int | None = None, image: str | None = None, scale: float = 2.0, crop: str | None = None,
           zoom: float = 1.0, grid: float | None = None, marks: str | None = None, polygon: str | None = None,
           out: str | None = None) -> Path:
    """Render a page or embedded image, optionally cropped/zoomed, with a pixel grid and markers.

    Grid labels, crop boxes, markers and polygons all use the *source* pixel coordinates
    (embedded image: native pixels; page: pixels of the page render at `scale`).
    """
    from PIL import Image, ImageDraw

    if image:
        src_path = run.path(image)
        if not src_path.exists():
            src_path = run.inspect / image
        im = Image.open(src_path).convert("RGB")
        stem = src_path.stem
    elif page:
        import pypdfium2 as pdfium

        im = pdfium.PdfDocument(str(run.report_path))[page - 1].render(scale=scale).to_pil().convert("RGB")
        stem = f"page_{page:02d}_s{scale:g}"
    else:
        raise SystemExit("render needs --page or --image")
    x0, y0 = 0.0, 0.0
    box = _parse_box(crop)
    if box:
        x0, y0, x1, y1 = box
        im = im.crop((int(x0), int(y0), int(x1), int(y1)))
    if zoom != 1:
        im = im.resize((int(im.width * zoom), int(im.height * zoom)), Image.LANCZOS)
    draw = ImageDraw.Draw(im)

    def to_canvas(x, y):
        return (x - x0) * zoom, (y - y0) * zoom

    if grid:
        gx = (int(x0 // grid) + 1) * grid
        while (gx - x0) * zoom < im.width:
            cx, _ = to_canvas(gx, y0)
            draw.line([(cx, 0), (cx, im.height)], fill=(255, 0, 255), width=1)
            draw.text((cx + 2, 2), f"{gx:g}", fill=(255, 0, 255))
            gx += grid
        gy = (int(y0 // grid) + 1) * grid
        while (gy - y0) * zoom < im.height:
            _, cy = to_canvas(x0, gy)
            draw.line([(0, cy), (im.width, cy)], fill=(0, 160, 255), width=1)
            draw.text((2, cy + 2), f"{gy:g}", fill=(0, 160, 255))
            gy += grid
    for k, (x, y) in enumerate(_parse_points(marks)):
        cx, cy = to_canvas(x, y)
        draw.ellipse([cx - 5, cy - 5, cx + 5, cy + 5], outline=(255, 0, 0), width=2)
        draw.text((cx + 7, cy - 7), str(k + 1), fill=(255, 0, 0))
    poly = _parse_points(polygon)
    if len(poly) >= 2:
        draw.line([to_canvas(x, y) for x, y in poly + [poly[0]]], fill=(255, 0, 0), width=2)
    name = out or f"{stem}{'_crop' if box else ''}{'_grid' if grid else ''}{'_mark' if marks or polygon else ''}.png"
    target = run.inspect / name
    im.save(target)
    return target


def search_text(run: Run, pattern: str, context: int = 1, page: int | None = None) -> list[dict]:
    rx = re.compile(pattern, re.I)
    hits = []
    for p in run.pages():
        if page and p["pdf_page"] != page:
            continue
        lines = p["text"].splitlines()
        for i, ln in enumerate(lines):
            if rx.search(ln) or rx.search(re.sub(r"\s+", " ", ln)):
                hits.append(dict(pdf_page=p["pdf_page"], printed_page=p["printed_page"], line=i + 1,
                                 text="\n".join(lines[max(0, i - context) : i + context + 1])))
    return hits


# ---------------------------------------------------------------------------- quotations

_CHARMAP = str.maketrans({"‐": "-", "‑": "-", "–": "-", "—": "-", "„": '"', "“": '"', "”": '"', "«": '"', "»": '"',
                          "‚": "'", "‘": "'", "’": "'", "­": "-", " ": " "})


def _normalize(text: str) -> str:
    text = text.translate(_CHARMAP)
    text = re.sub(r"(?<=[^\W\d_])-\s*\n\s*(?=[a-zäöüßàâçéèêëîïôûù])", "", text)  # de-hyphenate line breaks
    return re.sub(r"\s+", " ", text).strip()


def _soft(text: str) -> str:
    """Remove soft line-break hyphens kept as 'Zubrin- ger' in already re-flowed text."""
    return re.sub(r"(?<=[^\W\d_])-\s+(?=[a-zäöüßàâçéèêëîïôûù])", "", text)


def _squash(text: str) -> str:
    """Matching key: no whitespace and no double-quote marks (LLM clients often strip quote marks from text)."""
    return re.sub(r'[\s"]+', "", text)


class _Compact:
    """Whitespace- and quote-mark-free view of a normalized string with index mapping back to it."""

    def __init__(self, norm: str):
        self.norm = norm
        self.pos = [i for i, c in enumerate(norm) if not c.isspace() and c != '"']
        self.s = "".join(norm[i] for i in self.pos)

    def find(self, needle: str, start: int = 0) -> int:
        return self.s.find(_squash(_normalize(needle)), start)

    def slice(self, a: int, b: int) -> str:
        return self.norm[self.pos[a] : self.pos[b - 1] + 1]


def _boilerplate(pages: list[dict]) -> set[str]:
    """Running headers/footers: first/last lines that recur (digits ignored) on many pages."""
    cnt = collections.Counter()
    for p in pages:
        lines = [ln.strip() for ln in p["text"].splitlines() if ln.strip()]
        for ln in set(lines[:3] + lines[-3:]):
            cnt[re.sub(r"\d+", "#", ln)] += 1
    need = max(3, int(0.4 * len(pages)))
    return {k for k, n in cnt.items() if n >= need}


def _strip_boilerplate(text: str, boiler: set[str]) -> str:
    lines = text.splitlines()
    keep = [ln for i, ln in enumerate(lines) if not ((i < 4 or i >= len(lines) - 4) and re.sub(r"\d+", "#", ln.strip()) in boiler)]
    return "\n".join(keep)


def _page_views(run: Run):
    cache = getattr(run, "_page_views", None)
    if cache is None:
        pages = run.pages()
        boiler = _boilerplate(pages)
        cache = [(p, _Compact(_normalize(p["text"])), _normalize(_strip_boilerplate(p["text"], boiler)),
                  _Compact(_soft(_normalize(p["text"])))) for p in pages]
        run._page_views = cache
    return cache


def resolve_quote(run: Run, q: dict | str) -> dict:
    """Locate a quotation in the report text.

    q: "exact text" | {"text": ...} | {"start": ..., "end": ...}; optional "pdf_page"/"printed_page" limits the search.
    Matching ignores whitespace, line-break hyphenation and typographic quote/dash variants; the returned
    text is the report wording with whitespace normalized.
    """
    if isinstance(q, str):
        q = {"text": q}
    views = _page_views(run)
    want = None
    if q.get("pdf_page"):
        want = int(q["pdf_page"])
    elif q.get("printed_page"):
        want = run.pdf_page(int(q["printed_page"]))

    def locate(view: _Compact, soft: bool = False):
        prep = (lambda t: _soft(_normalize(t))) if soft else _normalize
        if q.get("text"):
            needle = _squash(prep(q["text"]))
            a = view.s.find(needle)
            return (a, a + len(needle)) if a >= 0 and needle else None
        sa = _squash(prep(q["start"]))
        se = _squash(prep(q["end"]))
        a = view.s.find(sa)
        if a < 0:
            return None
        e = view.s.find(se, a + len(sa) - len(se) if len(se) < len(sa) else a)
        return (a, e + len(se)) if e >= 0 else None

    order = [v for v in views if want is None or v[0]["pdf_page"] == want] + [v for v in views if want is not None and v[0]["pdf_page"] != want]
    for soft in (False, True):
        for p, view, _, softview in order:
            v = softview if soft else view
            span = locate(v, soft)
            if span:
                return dict(text=v.slice(*span), pdf_page=p["pdf_page"], printed_page=p["printed_page"], verified=True,
                            page_hint_ok=(want is None or p["pdf_page"] == want))
    # quotations crossing a page break (running headers/footers removed)
    for (p1, _, s1, _a), (p2, _, s2, _b) in zip(views, views[1:]):
        if want is not None and want not in (p1["pdf_page"], p2["pdf_page"]):
            continue
        joined = _Compact(_soft(s1 + " " + s2))
        span = locate(joined, True)
        if span:
            return dict(text=joined.slice(*span), pdf_page=p1["pdf_page"], pdf_page_end=p2["pdf_page"],
                        printed_page=p1["printed_page"], printed_page_end=p2["printed_page"], verified=True, page_hint_ok=True)
    return dict(text=q.get("text") or f"{q.get('start')} … {q.get('end')}", verified=False,
                pdf_page=want, printed_page=run.printed(want) if want else None)


def caption_for(run: Run, figure: str, kind_hint: str | None = None) -> dict | None:
    caps = read_json(run.raw / "report_captions.json", [])
    fig = str(figure)
    for c in caps:
        if c["number"] == fig and (kind_hint is None or c["kind"].lower().startswith(kind_hint.lower()[:3])):
            return c
    return None


# ---------------------------------------------------------------------------- geographic clue ledger

DEFAULT_CLUE_PATTERNS = [
    r"\bhm\s*\d", r"\bkm\s*\d", r"\d\s*(?:m³|m3|ha|km²|km2|m²|mm|cm)\b", r"\d[\d.,]*\s*m\b", r"\d+\s*%",
    r"\b\d{1,2}[.:]\d{2}\s*(?:Uhr|h|MESZ|MEZ|UTC|CET|CEST)\b",
    r"\b(?:orograf|orograph|links|rechts|linksufrig|rechtsufrig|flussab|flussauf|bachab|bachauf|oberhalb|unterhalb|nördlich|südlich|östlich|westlich|Mündung|Brücke|Straße|Strasse|Landesstraße|Gemeindestraße|Zubringer|Schwemmkegel|Talboden)",
    r"\b(?:upstream|downstream|left bank|right bank|north|south|east|west|confluence|bridge|road|tributary|fan|valley floor)\b",
    r"\b(?:rive gauche|rive droite|amont|aval|pont|route|confluence|cône)\b",
]


def gazetteer_names(run: Run, min_len: int = 4) -> dict[str, set[str]]:
    """Names found in local name-bearing layers (official + OSM)."""
    fields = ("NAME", "name", "GEW_NAME", "GEW_NAME_HAUPT", "HOFNAME", "STRNAME", "STRASSENNAME", "BEZ_ORTSTEIL",
              "NAMEEZGWLV", "GEMNAM", "GEMEINDENAME", "NAMENVARIANTE")
    names: dict[str, set[str]] = collections.defaultdict(set)
    for lyr in run.layer_names():
        if lyr.startswith("digitized:"):
            continue
        try:
            g = run.layer(lyr)
        except Exception:
            continue
        for f in fields:
            if f in g.columns:
                for v in g[f].dropna().astype(str).unique():
                    v = re.sub(r"\s*\(\d+\)\s*$", "", v).strip()
                    if len(v) >= min_len and not v.isdigit():
                        names[v].add(f"{lyr}.{f}")
    return names


def clues(run: Run, extra_terms: list[str] | None = None, context_lines: int = 1) -> Path:
    """Write outputs/geographic_clues.csv: exact passages with location/quantity/time clues and gazetteer matches."""
    pats = [re.compile(p, re.I) for p in DEFAULT_CLUE_PATTERNS + [re.escape(t) for t in (extra_terms or [])]]
    gaz = gazetteer_names(run)
    gaz_rx = [(n, re.compile(r"(?<!\w)" + re.escape(n) + r"(?!\w)", re.I)) for n in sorted(gaz, key=len, reverse=True)]
    rows = []
    for p in run.pages():
        lines = p["text"].splitlines()
        spans = []
        for i, ln in enumerate(lines):
            if any(rx.search(ln) for rx in pats) or any(rx.search(ln) for _, rx in gaz_rx):
                lo, hi = max(0, i - context_lines), min(len(lines), i + context_lines + 1)
                if spans and lo <= spans[-1][1]:
                    spans[-1] = (spans[-1][0], max(hi, spans[-1][1]))
                else:
                    spans.append((lo, hi))
        for k, (lo, hi) in enumerate(spans):
            passage = "\n".join(lines[lo:hi]).strip()
            if not passage:
                continue
            terms = sorted({m.group(0).strip() for rx in pats for m in rx.finditer(passage)})
            gm = sorted({f"{n} [{', '.join(sorted(gaz[n]))}]" for n, rx in gaz_rx if rx.search(passage)})
            rows.append(dict(clue_id=f"p{p['printed_page']}_{k + 1}", pdf_page=p["pdf_page"], printed_page=p["printed_page"],
                             passage=passage, matched_terms="; ".join(terms), gazetteer_matches="; ".join(gm)))
    target = run.out / "geographic_clues.csv"
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["clue_id", "pdf_page", "printed_page", "passage", "matched_terms", "gazetteer_matches"])
        w.writeheader()
        w.writerows(rows)
    # also a compact list of gazetteer names that occur anywhere in the report
    full = "\n".join(p["text"] for p in run.pages())
    found = {n: sorted(src) for n, rx in gaz_rx if rx.search(full) for src in [gaz[n]]}
    write_json(run.raw / "gazetteer_hits.json", found)
    return target


# ---------------------------------------------------------------------------- text from pipeline artifacts


def pages_from_pipeline(run_dir) -> list[dict]:
    """Pages (original text, English translation, file, page) from a multi_hazard_pipeline run folder."""
    from pathlib import Path

    d = Path(run_dir)
    src = read_json(d / "translated.json") or read_json(d / "source.json")
    if not src:
        raise SystemExit(f"No translated.json/source.json in {d}")
    groups: dict = {}
    order = []
    for c in src["chunks"]:
        key = (c.get("file") or c.get("filename") or "", str(c.get("page") or c.get("parent_chunk_id") or c["chunk_id"]))
        if key not in groups:
            groups[key] = {"file": key[0], "page": c.get("page"), "chunk_ids": [], "text": [], "translated_text": []}
            order.append(key)
        g = groups[key]
        g["chunk_ids"].append(c["chunk_id"])
        g["text"].append(c.get("text", ""))
        g["translated_text"].append(c.get("translated_text") or c.get("text", ""))
    pages = []
    for i, key in enumerate(order):
        g = groups[key]
        pages.append(dict(pdf_page=i + 1, file=g["file"], file_page=g["page"], chunk_ids=g["chunk_ids"],
                          text="\n".join(g["text"]), translated_text="\n".join(g["translated_text"])))
    return pages


def import_text(run: Run, pages: list[dict]) -> dict:
    """Use already extracted report text (e.g. from the pipeline) instead of reading the PDF. No figures are extracted."""
    run.ensure_dirs()
    prefixes = run.config.get("caption_prefixes") or DEFAULT_CAPTION_PREFIXES
    cap_rx = re.compile(r"\s+(?=(?:" + "|".join(re.escape(x) for x in prefixes) + r")\s*\d+[a-z]?\s*[:.])")

    def relines(t: str) -> str:
        if t.count("\n") > 3:
            return t
        t = cap_rx.sub("\n", t)
        t = re.sub(r"\s*\b((?:Seite|Page)\s+\d{1,4})\b\s*", r"\n\1\n", t)
        return re.sub(r"(?<=[.!?])\s+(?=[A-ZÄÖÜ0-9])", "\n", t)  # one sentence per line

    texts = [relines(p["text"]) for p in pages]
    if run.config.get("printed_page_offset") is None:
        off, votes = _detect_offset(texts)
        run.config["printed_page_offset"] = off
        run.config["printed_page_offset_votes"] = votes
    run.config["text_source"] = "pipeline artifacts (no PDF parsing, no figures)"
    run.save_config()
    out = []
    for i, p in enumerate(pages):
        rec = dict(p)
        rec["text"] = texts[i]
        rec["pdf_page"] = i + 1
        rec["printed_page"] = run.printed(i + 1)
        out.append(rec)
    write_json(run.dir / REPORT_TEXT, out)
    write_json(run.raw / "report_images.json", [])
    write_json(run.raw / "report_tables.json", [])
    caps = _captions(run, texts)
    write_json(run.raw / "report_captions.json", caps)
    return dict(pages=len(out), offset=run.page_offset, captions=len(caps))
