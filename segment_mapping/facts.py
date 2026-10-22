"""Deterministic facts for constrained (LLM-orchestrated) localization: name mentions, candidates, stream/catchment facts.

Nothing here calls a model. Every number a model may need (lengths, confluence distances, areas, elevations)
is computed by code and handed over as text, so the model only chooses among candidates.
"""
from __future__ import annotations

import re
import unicodedata
from collections import Counter, defaultdict


from .geom import Evaluator, as_line
from .report import DEFAULT_CLUE_PATTERNS
from .runctx import Run

KIND_PREFIX = {"streams": "S", "catchment": "C", "bridges": "B", "roads": "R", "names": "P", "road_km": "K", "buildings": "H"}
GENERIC = {"zubringer", "bach", "graben", "bruecke", "brucke", "strasse", "weg", "haus", "hof", "alm", "see", "tal", "berg",
           "wald", "feld", "wiese", "kapelle", "kirche", "schule", "gasthof", "hotel", "parkplatz", "runse", "rinne", "quelle",
           "stream", "river", "bridge", "road", "creek", "tributary", "unnamed", "none", "nan"}
WORD = re.compile(r"[^\W_]+(?:-[^\W_]+)*", re.UNICODE)


def norm(s: str) -> str:
    s = str(s).casefold().replace("ä", "ae").replace("ö", "oe").replace("ü", "ue").replace("ß", "ss")
    s = unicodedata.normalize("NFKD", s)
    return "".join(c for c in s if c.isalnum() or c.isspace()).strip()


def clean_name(v) -> str:
    return re.sub(r"\s*\([^)]*\)\s*$", "", str(v)).strip()


def _edit1(a: str, b: str) -> bool:
    if abs(len(a) - len(b)) > 1 or a == b:
        return a == b
    if len(a) > len(b):
        a, b = b, a
    i = j = d = 0
    while i < len(a) and j < len(b):
        if a[i] != b[j]:
            d += 1
            if d > 1:
                return False
            if len(a) == len(b):
                i += 1
            j += 1
        else:
            i += 1
            j += 1
    return d + (len(b) - j) <= 1


def _variants(n: str) -> set[str]:
    out = {n}
    if "ei" in n:
        out.add(n.replace("ei", "ai"))
    if "ai" in n:
        out.add(n.replace("ai", "ei"))
    return out


def _token_match(tok: str, name: str) -> str | None:
    """Return the match type of a normalized text token against a normalized single-word name."""
    variants = [name] + sorted(_variants(name) - {name})  # the name itself first, then spelling variants
    for kind, test in (("exact", lambda v: tok == v),
                       ("inflected", lambda v: tok.startswith(v) and len(v) >= 4 and len(tok) - len(v) <= 3),
                       ("compound", lambda v: tok.startswith(v) and len(v) >= 6),
                       ("fuzzy", lambda v: len(v) >= 7 and _edit1(tok, v))):
        if any(test(v) for v in variants):
            return kind
    return None


def _dehyphenate(text: str) -> str:
    return re.sub(r"(?<=[^\W\d_])-\s+(?=[a-zäöüß])", "", text)


def page_tokens(text: str):
    t = _dehyphenate(text)
    return t, [(m.start(), m.end(), norm(m.group(0))) for m in WORD.finditer(t)]


def find_mentions(pages: list[dict], names: dict[str, list[str]], strict: set[str] | None = None) -> dict[str, list[dict]]:
    """names: display name -> candidate keys. Returns display name -> [{page, match, snippet}].

    A capitalised name only matches capitalised words. Names in `strict` (place/field names, which often collide
    with common words) match only exactly or with a short inflection, never fuzzily or as a compound part.
    """
    strict = strict or set()
    out: dict[str, list[dict]] = defaultdict(list)
    prepared = []
    for name in names:
        parts = norm(name).split()
        if not parts or (len(parts) == 1 and ((len(parts[0]) < 4 and not any(ch.isdigit() for ch in parts[0])) or parts[0] in GENERIC)):
            continue
        prepared.append((name, parts))
    for p in pages:
        text, toks = page_tokens(p["text"])
        for name, parts in prepared:
            n = len(parts)
            for i in range(len(toks) - n + 1):
                kinds = [_token_match(toks[i + k][2], parts[k]) if k == n - 1 else
                         ("exact" if toks[i + k][2] == parts[k] or toks[i + k][2].startswith(parts[k][:-1]) and len(parts[k]) > 3 else None)
                         for k in range(n)]
                if all(kinds):
                    a, b = toks[i][0], toks[i + n - 1][1]
                    if name[:1].isupper() and not text[a:a + 1].isupper():
                        continue
                    if name in strict and kinds[-1] in ("fuzzy", "compound"):
                        continue
                    out[name].append(dict(page=p["pdf_page"], match=kinds[-1], text=text[a:b],
                                          snippet=text[max(0, a - 90): b + 90].replace("\n", " ")))
    return out


def quoted_local_names(pages: list[dict]) -> Counter:
    """Proper names written in quotation marks (often local names absent from official gazetteers)."""
    c = Counter()
    for p in pages:
        for m in re.finditer(r"[„\"“»«']\s*([A-ZÄÖÜ][\w\-äöüß ]{2,30}?)\s*[“\"”«»']", _dehyphenate(p["text"])):
            c[m.group(1).strip()] += 1
    return c


def numeric_clues(pages: list[dict], limit: int = 120) -> list[dict]:
    rx = [re.compile(p, re.I) for p in DEFAULT_CLUE_PATTERNS[:6]]
    out = []
    for p in pages:
        text = _dehyphenate(p["text"])
        for r in rx:
            for m in r.finditer(text):
                out.append(dict(page=p["pdf_page"], match=m.group(0), snippet=text[max(0, m.start() - 110): m.end() + 110].replace("\n", " ")))
    seen, uniq = set(), []
    for c in out:
        k = (c["page"], c["snippet"][:60])
        if k not in seen:
            seen.add(k)
            uniq.append(c)
    return uniq[:limit]


def _km_values(pages):
    vals = set()
    for p in pages:
        for m in re.finditer(r"\bkm\s*(\d{1,3}(?:[.,]\d{1,3})?)", p["text"], re.I):
            vals.add(round(float(m.group(1).replace(",", ".")), 3))
    return vals


def _same_feature(a, b, tol: float = 5.0) -> bool:
    """Two layers supplying the same geometry (e.g. a supplied and a context copy of the stream network)."""
    if a.is_empty or b.is_empty or a.geom_type.replace("Multi", "") != b.geom_type.replace("Multi", ""):
        return False
    size = (lambda g: g.area) if "Polygon" in a.geom_type else (lambda g: g.length)
    sa, sb = size(a), size(b)
    if sa and sb and abs(sa - sb) > 0.02 * max(sa, sb):
        return False
    return a.hausdorff_distance(b) <= tol


class Facts:
    def __init__(self, run: Run, spec: dict | None = None):
        self.run = run
        self.ev = Evaluator(run, spec or {})
        self.layers = (run.config.get("package") or {}).get("layers", {})
        self.pages = run.pages()
        self.candidates: dict[str, dict] = {}
        self.mentions: dict[str, list[dict]] = {}

    # ------------------------------------------------------------------ helpers
    def _layers_with(self, role):
        return [(n, v) for n, v in self.layers.items() if v.get("role") == role and n in self.run.layer_names()]

    def _add(self, kind, name, layer, expr, extra=None):
        prefix = KIND_PREFIX[kind]
        cid = f"{prefix}{sum(1 for c in self.candidates if c.startswith(prefix)) + 1}"
        self.candidates[cid] = dict(id=cid, kind=kind, name=name, layer=layer, expr=expr, **(extra or {}))
        return cid

    # ------------------------------------------------------------------ build
    def build(self, max_per_kind: int = 40) -> dict:
        names_index: dict[str, list[tuple]] = defaultdict(list)
        groups: dict[tuple, dict] = {}
        for role in ("catchment", "streams", "bridges", "roads", "names"):
            for lname, info in self._layers_with(role):
                g = self.run.layer(lname)
                nf, idf = info.get("name_field"), info.get("id_field")
                if not nf or nf not in g.columns:
                    if role == "catchment":  # unnamed provided outline: still a candidate
                        for _, r in g.iterrows():
                            gk = (role, lname, ("#", int(r["fid_"])))
                            groups[gk] = dict(role=role, layer=lname, name=f"{lname} #{int(r['fid_'])}", fids=[int(r["fid_"])], id=None, n=1)
                    continue
                key_cols = [nf] + ([idf] if idf and idf in g.columns and role in ("catchment", "streams") else [])
                for keyvals, sub in g.groupby(key_cols, dropna=False):
                    keyvals = keyvals if isinstance(keyvals, tuple) else (keyvals,)
                    if keyvals[0] is None or str(keyvals[0]) in ("nan", "None"):
                        continue
                    disp = clean_name(keyvals[0])
                    if not disp or norm(disp) in GENERIC:
                        continue
                    code = keyvals[1] if len(keyvals) > 1 and str(keyvals[1]) not in ("nan", "None") else None
                    gk = (role, lname, keyvals)
                    groups[gk] = dict(role=role, layer=lname, name=disp, fids=[int(v) for v in sub["fid_"]],
                                      id=str(code) if code is not None else None, n=len(sub))
                    names_index[disp].append(gk)
                ref = info.get("ref_field")
                if role == "roads" and ref and ref in g.columns:
                    for rv, sub in g.groupby(ref, dropna=True):
                        rv = str(rv).strip()
                        if rv and re.fullmatch(r"[A-Z]{1,3}\s?\d{1,4}[a-z]?", rv):
                            gk = (role, lname, ("ref", rv))
                            groups[gk] = dict(role=role, layer=lname, name=rv, fids=[int(v) for v in sub["fid_"]], id=rv, n=len(sub), ref_field=ref)
                            names_index[rv].append(gk)
        strict = {groups[gk]["name"] for gk in groups if gk[0] == "names"}
        self.mentions = find_mentions(self.pages, names_index, strict)
        mentioned = {gk for n in self.mentions for gk in names_index.get(n, [])}

        # catchments: all in the area (usually few) ; streams: mentioned + those inside the provided catchments
        from shapely.ops import unary_union

        def geom_of(info):
            return self.run.layer(info["layer"]).set_index("fid_").loc[info["fids"]].geometry.union_all()

        small_layers = {n for n, _ in self._layers_with("catchment") if len(self.run.layer(n)) <= 3}
        chosen = [gk for gk, info in groups.items()
                  if gk in mentioned or (info["role"] == "catchment" and info["layer"] in small_layers)]
        cg = [geom_of(groups[gk]) for gk in chosen if groups[gk]["role"] == "catchment" and groups[gk]["layer"] in small_layers]
        catch_union = unary_union(cg) if cg else None
        chosen_streams = [geom_of(groups[gk]) for gk in chosen if groups[gk]["role"] == "streams"]
        for gk, info in groups.items():
            if gk in chosen:
                continue
            if info["role"] == "streams":
                geom = geom_of(info)
                if catch_union is not None and geom.intersection(catch_union).length > 0.3 * geom.length:
                    chosen.append(gk)
                elif geom.length > 1000 and any(geom.distance(s) < 30 for s in chosen_streams):  # receiving river
                    chosen.append(gk)
            elif info["role"] == "bridges" and chosen_streams:
                if any(geom_of(info).distance(s) < 15 for s in chosen_streams):
                    chosen.append(gk)
        # place/field names identical to a chosen stream or catchment name label that feature; they are not places
        feature_names = {norm(groups[gk]["name"]) for gk in chosen if groups[gk]["role"] in ("streams", "catchment")}
        chosen = [gk for gk in chosen if not (groups[gk]["role"] == "names" and norm(groups[gk]["name"]) in feature_names)]
        per_kind = Counter()
        placed: list[tuple] = []
        kept: list[tuple] = []  # (role, geometry, candidate id): the same feature supplied by several layers is one candidate
        for gk in sorted(chosen, key=lambda k: (k[0], -len(self.mentions.get(groups[k]["name"], [])))):
            info = groups[gk]
            role = info["role"]
            if role == "names":
                pt = geom_of(info).representative_point()
                if any(norm(n) == norm(info["name"]) and pt.distance(q) < 100 for n, q in placed):
                    continue
                placed.append((info["name"], pt))
            else:
                geom = geom_of(info)
                twin = next((cid for r, g, cid in kept if r == role and _same_feature(g, geom)), None)
                if twin:
                    c = self.candidates[twin]
                    if norm(info["name"]) != norm(c["name"]) and info["name"] not in c.setdefault("aliases", []):
                        c["aliases"].append(info["name"])
                    pages = {m["page"] for m in self.mentions.get(info["name"], [])}
                    c["mention_pages"] = sorted(set(c["mention_pages"]) | pages)
                    c["mention_count"] += len(self.mentions.get(info["name"], []))
                    continue
            if per_kind[role] >= max_per_kind:
                continue
            per_kind[role] += 1
            if info.get("ref_field"):
                base = {"layer": info["layer"], "where": {info["ref_field"]: info["name"]}}
            else:
                base = {"layer": info["layer"], "ids": info["fids"]}
            expr = {"stream": base} if role == "streams" else (base | {"merge": "lines"} if role == "roads" else base)
            pages = sorted({m["page"] for m in self.mentions.get(info["name"], [])})
            cid = self._add(role, info["name"], info["layer"], expr, dict(code=info.get("id"), mention_pages=pages,
                                                                          mention_count=len(self.mentions.get(info["name"], []))))
            if role != "names":
                kept.append((role, geom, cid))
        # road kilometre markers whose value is mentioned
        kms = _km_values(self.pages)
        for lname, info in self._layers_with("road_km"):
            g = self.run.layer(lname)
            vf, rf = info.get("value_field"), info.get("ref_field")
            if not vf or vf not in g.columns:
                continue
            for _, r in g.iterrows():
                try:
                    v = round(float(str(r[vf]).replace(",", ".")), 3)
                except ValueError:
                    continue
                if v in kms:
                    road = str(r[rf]) if rf and rf in g.columns else ""
                    self._add("road_km", f"{road} km {r[vf]}".strip(), lname, {"layer": lname, "ids": [int(r["fid_"])]},
                              dict(code=road, mention_pages=[], mention_count=0))
        self._describe()
        return self.to_dict()

    # ------------------------------------------------------------------ facts
    def _describe(self):
        streams = {}
        for cid, c in self.candidates.items():
            try:
                g = self.ev.geom(c["expr"])
            except Exception as exc:
                c["description"] = f"(geometry error: {exc})"
                continue
            c["geom_type"] = g.geom_type
            if c["kind"] == "streams":
                ln = as_line(g)
                dem = self.run.dem()
                z0, z1 = dem.sample(*ln.coords[0]), dem.sample(*ln.coords[-1])
                streams[cid] = ln
                c["facts"] = dict(length_m=round(ln.length, 1), mouth_elev_m=round(z0, 1), source_elev_m=round(z1, 1))
            elif g.geom_type in ("Polygon", "MultiPolygon"):
                c["facts"] = dict(area_ha=round(g.area / 1e4, 3))
                if c["kind"] == "catchment":
                    s = self.run.dem().stats(g)
                    c["facts"].update(area_km2=round(g.area / 1e6, 3), elev_min_m=round(s.get("min", float("nan")), 1),
                                      elev_max_m=round(s.get("max", float("nan")), 1))
            else:
                c["facts"] = dict(length_m=round(g.length, 1)) if g.length else {}
        # confluences: tributary mouth (line start) within 30 m of another stream
        for a, la in streams.items():
            for b, lb in streams.items():
                if a == b:
                    continue
                from shapely.geometry import Point

                mouth_b = Point(lb.coords[0])
                if mouth_b.distance(la) <= 30:
                    d = la.project(mouth_b)
                    if d < 30 and la.length <= lb.length:  # shared end point of clipped lines, not a confluence
                        continue
                    self.candidates[b].setdefault("facts", {})["flows_into"] = a
                    self.candidates[a].setdefault("facts", {}).setdefault("tributaries", []).append(
                        dict(stream=b, name=self.candidates[b]["name"], at_m_from_mouth=round(d, 1), at_hm=round(d / 100, 2)))
        # catchment membership of streams
        for cid, c in self.candidates.items():
            if c["kind"] != "catchment":
                continue
            try:
                cg = self.ev.geom(c["expr"])
            except Exception:
                continue
            inside = [s for s, ln in streams.items() if ln.intersection(cg).length > 0.5 * ln.length]
            c.setdefault("facts", {})["streams_inside"] = inside
        mentioned_streams = {s: ln for s, ln in streams.items() if self.candidates[s].get("mention_count")}
        for cid, c in self.candidates.items():
            if c["kind"] in ("streams", "catchment") or not mentioned_streams:
                continue
            try:
                p = self.ev.geom(c["expr"]).representative_point()
            except Exception:
                continue
            s, ln = min(mentioned_streams.items(), key=lambda kv: kv[1].distance(p))
            c.setdefault("facts", {}).update(near_stream=s, along_m_from_mouth=round(ln.project(p), 1),
                                             lateral_m=round(ln.distance(p), 1), xy=[round(p.x), round(p.y)])
        # bridges: which stream they cross (and where), and which road they carry. Lets the model identify a bridge
        # the report calls by a local name ("bridge Lochmure") as the only road bridge over that stream.
        roads = {cid: self.ev.geom(c["expr"]) for cid, c in self.candidates.items() if c["kind"] == "roads"}
        for cid, c in self.candidates.items():
            if c["kind"] != "bridges":
                continue
            f = c.setdefault("facts", {})
            try:
                g = self.ev.geom(c["expr"])
            except Exception:
                continue
            crossing = [(s, ln) for s, ln in streams.items() if ln.distance(g) <= 15]
            if crossing:
                s, ln = min(crossing, key=lambda kv: kv[1].distance(g))
                d = ln.project(g.representative_point())
                same = [o for o, oc in self.candidates.items() if oc["kind"] == "bridges" and o != cid
                        and ln.distance(self.ev.geom(oc["expr"])) <= 15]
                f["crosses"] = f"{s} at {d:.0f} m from its mouth (hm {d / 100:.2f})" + (
                    "; the only bridge candidate on this stream" if not same else f"; other bridges on it: {same}")
            carried = [r for r, rg in roads.items() if rg.distance(g) <= 15]
            if carried:
                f["carries_road"] = carried
        for cid, c in self.candidates.items():
            f = c.get("facts", {})
            bits = [f"{c['kind']} '{c['name']}'" + (f" [{c['code']}]" if c.get("code") else "")]
            if c.get("aliases"):
                bits.append(f"also named {c['aliases']}")
            first = ("crosses", "carries_road", "flows_into")
            bits += [f"{k}={f[k]}" for k in first if k in f]
            bits += [f"{k}={v}" for k, v in f.items() if k not in ("tributaries", *first)]
            if f.get("tributaries"):
                bits.append("tributaries: " + "; ".join(f"{t['stream']} {t['name']} at {t['at_m_from_mouth']} m (hm {t['at_hm']})" for t in f["tributaries"]))
            bits.append(f"mentioned on pages {c['mention_pages']}" if c.get("mention_pages") else "not mentioned in the report")
            c["description"] = ", ".join(bits)

    def to_dict(self) -> dict:
        quoted = quoted_local_names(self.pages)
        known = {norm(c["name"]) for c in self.candidates.values()}
        return dict(candidates=list(self.candidates.values()),
                    mentions={k: v[:12] for k, v in self.mentions.items()},
                    unresolved_quoted_names=[n for n, _ in quoted.most_common(30) if norm(n) not in known],
                    numeric_clues=numeric_clues(self.pages))


def evidence_bundle(run: Run, facts: dict, quotes: list[dict], process_text: str = "", max_pages: int = 3, page_chars: int = 6000) -> dict:
    """Pages, mentions and numeric clues relevant to one segment. quotes: [{pdf_page, quote}]."""
    pages = run.pages()
    wanted = [q["pdf_page"] for q in quotes if q.get("pdf_page")]
    if not wanted and process_text:
        words = {w for w in re.findall(r"[a-z]{4,}", process_text.casefold())}
        scored = sorted(pages, key=lambda p: -len(words & set(re.findall(r"[a-z]{4,}", (p.get("translated_text") or p["text"]).casefold()))))
        wanted = [p["pdf_page"] for p in scored[:2]]
    wanted = list(dict.fromkeys(wanted))[:max_pages]
    by_page = {p["pdf_page"]: p for p in pages}
    page_items = [dict(page=n, printed_page=by_page[n]["printed_page"],
                       english=(by_page[n].get("translated_text") or by_page[n]["text"])[:page_chars]) for n in wanted if n in by_page]
    cands = [dict(id=c["id"], name=c["name"], kind=c["kind"]) for c in facts["candidates"] if set(c.get("mention_pages", [])) & set(wanted)]
    clues = [c for c in facts["numeric_clues"] if c["page"] in wanted][:25]
    return dict(pages=page_items, candidates_on_pages=cands, numeric_clues=clues)
