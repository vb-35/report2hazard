"""Method menu for constrained localization: small JSON choices -> toolkit geometry expressions + classification.

The model never writes geometry. It picks a method, candidate ids, point tokens and numbers; this module builds
the expression, decides the placement class from the method, and applies rule 5d deterministically.
"""
from __future__ import annotations

import json
import re

METHODS = {
    "report_outline": "A verified, reviewed event outline from figure_candidates (args.candidate); preserve it without fitting or buffering.",
    "location_point": "An identified location only (args.center); native point, affected extent remains unknown.",
    "channel_reach": "An identified stream reach only (args.stream, from_point, to_point); native line, no inferred width.",
    "feature_geometry": "Identified candidate in its native geometry (args.candidate); this locates the feature, not its damage extent.",
    "catchment": "Whole catchment (or another catchment candidate C#). For conditions/triggers that concern the catchment as a whole.",
    "elevation_band": "Part of a catchment between elevations (args.min_elev_m / max_elev_m), e.g. 'above 1500 m'.",
    "reach_corridor": "Channel and banks along a stream between two points (args.stream, from_point, to_point), "
                      "terrain band up to a height above the channel (parameter = height_above_channel_m).",
    "valley_floor": "Valley floor along a river between two points (args.river, upstream, downstream), terrain band "
                    "(parameter = height_above_channel_m), optionally only one side (args.side; left/right looking downstream).",
    "feature": "One identified candidate feature (args.candidate): a catchment/area as is; a bridge or road line widened "
               "(parameter = width_m); a point given a radius (parameter = width_m used as diameter).",
    "road_stretch": "Stretch of a road candidate (args.candidate R#) of args.length_m centred on args.center; road land "
                    "parcels if available (args.use_road_land) else widened (parameter = width_m).",
    "buildings_near": "Building footprints within args.radius_m of args.center (candidates by proximity, never proof of damage). "
                      "Only for a building the report identifies so that one footprint fits.",
    "named_area": "Surroundings of a named place, bank or confluence the report uses only as 'in the area of X' "
                  "(args.center, args.radius_m 100-300 m). Always a search_area: where something happened, not its footprint.",
    "point_area": "Last resort: circle around a located point (args.center), parameter = radius_m. Placement 'hypothesis'.",
    "expression": "Raw toolkit geometry expression (args.expression as JSON text) when nothing else fits. Explain why.",
}
PARAMS = {"reach_corridor": ("height_above_channel_m", 3.0, (0.5, 1.5)), "valley_floor": ("height_above_channel_m", 3.0, (0.5, 1.5)),
          "feature": ("width_m", 6.0, (0.75, 1.25)), "road_stretch": ("width_m", 6.0, (0.75, 1.25)),
          "point_area": ("radius_m", 30.0, (0.5, 2.0))}
ARG_KEYS = ["candidate", "stream", "river", "from_point", "to_point", "upstream", "downstream", "center", "side",
            "min_elev_m", "max_elev_m", "max_dist_m", "length_m", "radius_m", "use_road_land", "within_catchment", "expression"]
PLACEMENT_REASON = {
    "report_outline": "documented: explicit event outline with reviewed event interpretation and independently checked map alignment",
    "location_point": "approximate: identified location; no affected-area boundary is inferred",
    "channel_reach": "approximate: identified channel reach; no affected-area width is inferred",
    "feature_geometry": "approximate: identified feature in its native geometry; affected extent remains unknown",
    "catchment": "approximate: official catchment feature named or identified by the report",
    "elevation_band": "modelled: DEM elevation threshold applied within the catchment",
    "reach_corridor": "modelled: terrain band along a calibrated stream reach (DEM calculation)",
    "valley_floor": "modelled: terrain band along the valley-floor river between named limits (DEM calculation)",
    "feature": "approximate: official/OSM feature identified from the report text",
    "road_stretch": "modelled: calculated window along an identified road",
    "buildings_near": "approximate: existing building footprints selected by distance to a located point",
    "named_area": "approximate: search area around a named place the report locates only by 'in the area of'",
    "point_area": "hypothesis: buffer around an inferred point with no method behind the extent",
    "expression": "modelled: custom toolkit expression",
}


class TemplateError(ValueError):
    pass


class Compiler:
    def __init__(self, facts: dict, frame: dict, layers: dict, chainage: bool, figure_candidates=None):
        self.cands = {c["id"]: c for c in facts["candidates"]}
        self.frame = frame
        self.layers = layers  # run.config["package"]["layers"]
        self.has_chainage = chainage
        self.figures = figure_candidates or {}
        self.named = {f["candidate"] for f in frame.get("named_features", []) if f.get("candidate") and f.get("certainty") == "named"}
        self.named.add(frame.get("catchment"))
        self.named.add(frame.get("main_stream"))
        self.near_checks: list[tuple[str, str]] = []  # (tributary, hm token): distance checked by the caller

    # ------------------------------------------------------------------ spec frame
    def refs(self) -> dict:
        r = {cid: c["expr"] for cid, c in self.cands.items()}
        r["main"] = self.frame["main_stream"]
        r["catch"] = self.frame["catchment"]
        if self.frame.get("receiving_river"):
            r["river"] = self.frame["receiving_river"]
        return r

    def chainages(self) -> dict:
        anchors = []
        for a in self.frame.get("chainage_anchors", []):
            if a["point"].strip() == "mouth":
                anchors.append({"value": float(a["value"]), "at": "start", "label": "mouth of the main stream"})
            else:
                anchors.append({"value": float(a["value"]), "point": self.point(a["point"], allow_hm=False),
                                "label": f"{a['point']} (report p. {a.get('page')})"})
        if not anchors:
            return {}
        if not any(x.get("at") == "start" for x in anchors):
            anchors.insert(0, {"value": 0.0, "at": "start", "label": "mouth of the main stream (assumed hm 0)"})
        return {"hm": {"line": "main", "unit_m": 100, "anchors": anchors}}

    # ------------------------------------------------------------------ tokens
    def _cand(self, cid: str, kinds=None) -> str:
        if cid in ("main", "catch", "river"):
            return cid
        if cid not in self.cands:
            raise TemplateError(f"unknown candidate id {cid!r}")
        if kinds and self.cands[cid]["kind"] not in kinds:
            raise TemplateError(f"{cid} is a {self.cands[cid]['kind']}, expected {kinds}")
        return cid

    def point(self, tok, allow_hm: bool = True, line: str = "main"):
        """Resolve a point token. 'mouth'/'source' refer to `line` (the stream a reach runs along), default the main stream."""
        if tok is None:
            raise TemplateError("missing point token")
        t = str(tok).strip()
        if t == "mouth":
            return {"start": line}
        if t == "source":
            return {"end": line}
        m = re.fullmatch(r"hm:\s*(-?\d+(?:[.,]\d+)?)", t)
        if m:
            if not (allow_hm and self.has_chainage):
                raise TemplateError("hm point tokens need chainage anchors in the frame")
            return {"chainage_point": {"chainage": "hm", "at": float(m.group(1).replace(",", "."))}}
        m = re.fullmatch(r"confluence:\s*(S\d+)", t)
        if m:
            sid = self._cand(m.group(1), ("streams",))
            if sid == line and line != "main":  # confluence of the tributary the reach runs along = its own mouth
                return {"start": line}
            if sid == self.frame.get("receiving_river"):
                return {"start": "main"}
            into = (self.cands[sid].get("facts") or {}).get("flows_into") or "main"
            if sid == self.frame["main_stream"]:
                return {"start": "main"}
            return {"endpoint_near": {"line": sid, "to": into}}
        m = re.fullmatch(r"(?:feature:|place:|km:)?\s*([A-Z]\d+)", t)
        if m:
            cid = self._cand(m.group(1))
            if self.cands[cid]["kind"] in ("streams", "catchment"):
                raise TemplateError(f"{cid} is a {self.cands[cid]['kind']}, not a point: use mouth, source, confluence:{cid} or a "
                                    "bridge/road/place candidate (for a whole stream use reach_corridor)")
            return {"representative_point": cid}
        m = re.fullmatch(r"building:\s*(\d+)", t)
        if m:
            return {"centroid": {"layer": self._layer("buildings"), "ids": [int(m.group(1))]}}
        m = re.fullmatch(r"xy:\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)", t)
        if m:
            return {"point": [float(m.group(1)), float(m.group(2))]}
        raise TemplateError(f"bad point token {t!r}; use mouth, source, hm:<value>, confluence:S#, <candidate id>, building:<fid>, xy:<x>,<y>")

    def _layer(self, role: str) -> str:
        for name, info in self.layers.items():
            if info.get("role") == role:
                return name
        raise TemplateError(f"the spatial package has no '{role}' layer")

    # ------------------------------------------------------------------ parts
    def compile_part(self, seg_id: str, part_id: str, ans: dict) -> dict:
        m = ans["method"]
        a = {k: ans.get("args", {}).get(k) for k in ARG_KEYS}
        figure = self.figures.get(a.get("candidate")) if m == "report_outline" else None
        if m == "report_outline" and (not figure or str(seg_id) not in figure["segments"]):
            raise TemplateError("report_outline must be a verified candidate explicitly linked to this segment")
        par = ans.get("parameter") or {}
        uses_param = m in PARAMS
        g, layers = self._geometry(m, a), set()
        if m == "feature":
            kind = self.cands[self._cand(a["candidate"])].get("geom_type", "")
            uses_param = "Polygon" not in kind
        report_area = par.get("report_area_ha")
        basis = par.get("basis") or "assumption"
        parameter, rule5d = None, False
        if uses_param:
            name, default, (lo, hi) = PARAMS[m]
            if report_area and par.get("fit_report_area", False) and m in ("reach_corridor", "valley_floor"):
                parameter = {"name": name, "unit": "m", "fit_area_ha": float(report_area), "bounds": [0, 60]}
            else:
                v = par.get("value")
                if v is None or v <= 0:
                    v, basis = default, "assumption"
                v = float(v)
                parameter = {"name": name, "unit": "m", "value": v, "sensitivity": [round(v * lo, 3), round(v * hi, 3)]}
                rule5d = basis == "assumption" and m != "point_area"
        placement = PLACEMENT_REASON[m].split(":")[0]
        if m == "feature" and rule5d:
            placement = "modelled"
        is_hypo, conf = int(ans.get("is_hypo", 1)), ans.get("confidence", "low")
        reasons = dict(reason_is_hypo=ans.get("reason_is_hypo", ""), reason_confidence=ans.get("reason_confidence", ""))
        if rule5d:
            is_hypo, conf = 1, "low"
            reasons = dict(reason_is_hypo="rule 5d: the report gives no extent for this part (parameter is an assumption); " + reasons["reason_is_hypo"],
                           reason_confidence="rule 5d: extent without basis in the report; " + reasons["reason_confidence"])
        if m == "point_area":
            is_hypo, conf = 1, "low"
            if parameter and parameter.get("value", 0) > 500:
                raise TemplateError("point_area radius above 500 m: an area that large is not a point. Use catchment, valley_floor "
                                    "or reach_corridor between named limits, or explain a smaller radius")
        self._check_classification(m, a, is_hypo, conf, report_area)
        quotes = [{"pdf_page": q.get("page"), "text": q.get("text", "")} for q in ans.get("quotes", []) if q.get("text")]
        basis_txt = f"{m}: " + ", ".join(f"{k}={v}" for k, v in a.items() if v not in (None, "", False) and k != "expression")
        if parameter:
            basis_txt += f"; {parameter['name']}=" + (f"fit to {parameter['fit_area_ha']} ha" if "fit_area_ha" in parameter else f"{parameter['value']} ({basis})")
        role = ans.get("geometry_role") or ("location" if m in ("location_point", "channel_reach", "feature_geometry") else "event_extent")
        if m in ("location_point", "channel_reach", "feature_geometry") and role != "location":
            raise TemplateError(f"{m} provides a location, not an event footprint: geometry_role must be location")
        boundary = ans.get("boundary_confidence", "unknown")
        if m == "named_area":  # the wording gives a place, never an extent
            role, boundary = "search_area", "unknown"
        location_confidence = ans.get("location_confidence", conf)
        if location_confidence == "high" and m != "report_outline":
            cid = a.get("candidate")
            if m == "catchment":
                cid = self.frame.get("catchment") if not cid or cid == "catch" else cid
            elif m == "location_point":
                cid = str(a.get("center", "")).removeprefix("feature:").removeprefix("place:")
            if m not in ("catchment", "feature", "feature_geometry", "location_point") or cid not in self.named:
                raise TemplateError("high location confidence requires a uniquely named GIS feature; use medium/low for inference or proximity")
        if role != "event_extent" and boundary != "unknown":
            raise TemplateError("a location/context/search area has no event boundary: boundary_confidence must be unknown")
        if boundary == "high" and m != "report_outline":
            raise TemplateError("high boundary confidence requires a verified report outline, not a reconstructed template")
        if rule5d and boundary not in ("low", "unknown"):
            raise TemplateError("assumed extent cannot have medium or high boundary confidence")
        result = {
            "part_id": part_id, "seg_id": seg_id, "part_label": ans.get("label", "")[:120], "geometry": g, "parameter": parameter,
            "placement": placement, "is_hypo": is_hypo, "confidence": conf, "rule_5d": rule5d,
            "geometry_role": role, "event_confidence": ans.get("event_confidence", "unknown"),
            "location_confidence": location_confidence, "boundary_confidence": boundary,
            "report_area_ha": report_area,
            "evidence_type": ans.get("evidence_type") or "text", "quotes": quotes,
            "geom_basis": basis_txt, "data_src": self._sources(m, a), "rep_value": ans.get("rep_value", ""),
            "rep_page": ",".join(str(q["pdf_page"]) for q in quotes if q.get("pdf_page")),
            "reasoning": ans.get("reasoning", "") + (f" Parameter justification: {par.get('justification')}" if par.get("justification") else ""),
            "uncertainty": ans.get("uncertainty", ""), "alternatives": ans.get("alternatives", "") or ("none identified" if not is_hypo else ""),
            "reason_placement": PLACEMENT_REASON[m] if not (m == "feature" and rule5d) else "modelled: identified feature widened by an assumed width",
            **reasons, "method": m, "method_args": {k: v for k, v in a.items() if v not in (None, "")},
        }
        if figure:
            interpretation = figure["interpretation"]
            result.update(geometry_role="event_extent", event_confidence=interpretation["event_confidence"],
                boundary_confidence=interpretation["boundary_confidence"], location_confidence="medium",
                geom_basis="Reviewed explicit outline; preserved without fitting. " + interpretation["reason"],
                data_src=str(figure["source"]), figures=[figure["figure_id"]],
                georeferencing_assessment=figure["georeferencing_assessment"],
                georeferencing_limitations=figure["georeferencing_assessment"]["accuracy_note"])
        return result

    def _check_classification(self, m, a, is_hypo, conf, report_area):
        if conf == "high":
            cid = a.get("candidate") or ("catch" if m == "catchment" else None)
            if m == "location_point":
                cid = str(a.get("center", "")).removeprefix("feature:").removeprefix("place:")
            cid = self.frame["catchment"] if cid == "catch" else cid
            if is_hypo != 0:
                raise TemplateError("confidence high requires is_hypo 0")
            if m not in ("catchment", "feature", "feature_geometry", "location_point") or cid not in self.named:
                raise TemplateError("confidence high is only for a feature the report names that matches exactly one GIS feature "
                                    "(frame certainty 'named'); use medium or low")
        if m == "buildings_near" and is_hypo == 0:
            raise TemplateError("buildings_near selects candidates by proximity: is_hypo must be 1 (proximity is not damage)")

    def _sources(self, m, a) -> str:
        ids = [a.get(k) for k in ("candidate", "stream", "river") if a.get(k)]
        names = [f"{self.cands[i]['layer']} {i} '{self.cands[i]['name']}'" for i in ids if i in self.cands]
        if m in ("catchment", "elevation_band"):
            c = self.cands.get(self.frame["catchment"])
            names.append(f"{c['layer']} '{c['name']}'" if c else "catchment")
        if m in ("elevation_band", "reach_corridor", "valley_floor"):
            names.append("DEM")
        if m == "buildings_near":
            names.append(self._layer("buildings"))
        return "; ".join(dict.fromkeys(names)) or "provided spatial package"

    def _geometry(self, m, a):
        P = "$param"
        if m == "report_outline":
            return self.figures[a["candidate"]]["geometry"]
        if m == "location_point":
            return self.point(a["center"])
        if m == "feature_geometry":
            return self._cand(a["candidate"])
        if m == "channel_reach":
            s = self._cand(a["stream"] or "main", ("streams",))
            f, t = a["from_point"], a["to_point"]
            if s == "main" and str(f).startswith("hm:") and str(t).startswith("hm:") and self.has_chainage:
                return {"reach": {"chainage": "hm", "from": float(str(f)[3:].replace(",", ".")),
                                  "to": float(str(t)[3:].replace(",", "."))}}
            return {"between": {"line": s, "from": self.point(f, line=s), "to": self.point(t, line=s)}}
        if m == "catchment":
            return self._cand(a["candidate"] or "catch", ("catchment",))
        if m == "elevation_band":
            within = self._cand(a["candidate"] or "catch", ("catchment",))
            if a["min_elev_m"] is None and a["max_elev_m"] is None:
                raise TemplateError("elevation_band needs min_elev_m and/or max_elev_m")
            return {"dem_mask": {"within": within, "min": a["min_elev_m"], "max": a["max_elev_m"]}}
        if m == "reach_corridor":
            s = self._cand(a["stream"] or "main", ("streams",))
            f, t = a["from_point"], a["to_point"]
            if s == "main" and str(f).startswith("hm:") and str(t).startswith("hm:") and self.has_chainage:
                line = {"reach": {"chainage": "hm", "from": float(str(f)[3:].replace(",", ".")), "to": float(str(t)[3:].replace(",", "."))}}
            else:
                # hm stations lie on the main stream; on a tributary they are projected onto it (e.g. hm 18.3 = its confluence)
                line = {"between": {"line": s, "from": self.point(f, line=s), "to": self.point(t, line=s)}}
                if s != "main":
                    self.near_checks.extend((s, tok) for tok in (f, t) if str(tok).startswith("hm:"))
            terr = {"channels": [line], "max_dist_m": float(a["max_dist_m"] or 50), "height_m": P}
            if a["within_catchment"]:
                terr["within"] = "catch"
            return {"terrain": terr}
        if m == "valley_floor":
            river = a["river"] or ("river" if self.frame.get("receiving_river") else None)
            if not river:
                raise TemplateError("valley_floor needs args.river (or a receiving_river in the frame)")
            river = self._cand(river, ("streams",))
            line = {"between": {"line": river, "from": self.point(a["upstream"]), "to": self.point(a["downstream"])}}
            self.near_checks.extend((river, tok) for tok in (a["upstream"], a["downstream"]) if str(tok).startswith("hm:"))
            within = {"buffer": {"geom": line, "distance": float(a["max_dist_m"] or 300), "cap": "flat"}}
            if a["side"]:
                # Streams and between() run mouth-to-source; bank names look downstream.
                side = {"left": "right", "right": "left"}.get(a["side"], a["side"])
                within = {"intersection": [within, {"half_plane": {"line": line, "side": side, "extent_m": 2000, "end_extension_m": 0}}]}
            return {"terrain": {"channels": [line], "max_dist_m": float(a["max_dist_m"] or 300), "within": within, "height_m": P}}
        if m == "feature":
            if a["center"] or a["length_m"]:
                raise TemplateError("feature uses the entire candidate and cannot apply center/length_m; "
                                    "use location_point for a known location or road_stretch for a reported length")
            cid = self._cand(a["candidate"])
            gt = self.cands[cid].get("geom_type", "")
            if "Polygon" in gt:
                return cid
            if "Point" in gt:
                return {"buffer": {"geom": cid, "width": P}}
            return {"buffer": {"geom": cid, "width": P, "cap": "flat"}}
        if m == "road_stretch":
            road = self._cand(a["candidate"], ("roads",))
            if not a["length_m"]:
                raise TemplateError("road_stretch needs length_m (use the report's length)")
            win = {"window": {"line": road, "center": self.point(a["center"]), "length_m": float(a["length_m"])}}
            if a["use_road_land"]:
                return {"intersection": [{"layer": self._layer("road_land")}, {"buffer": {"geom": win, "distance": 40, "cap": "flat"}}]}
            return {"buffer": {"geom": win, "width": P, "cap": "flat"}}
        if m == "buildings_near":
            if not a["radius_m"]:
                raise TemplateError("buildings_near needs radius_m")
            return {"layer": self._layer("buildings"), "within_distance": [self.point(a["center"]), float(a["radius_m"])]}
        if m == "named_area":
            r = a["radius_m"]
            if not r or not 50 <= r <= 500:
                raise TemplateError("named_area needs radius_m between 50 and 500 m (100-300 m for 'in the area of X')")
            return {"buffer": {"geom": self.point(a["center"]), "distance": float(r)}}
        if m == "point_area":
            return {"buffer": {"geom": self.point(a["center"]), "distance": P}}  # radius checked in compile_part
        if m == "expression":
            try:
                return json.loads(a["expression"])
            except (TypeError, json.JSONDecodeError) as exc:
                raise TemplateError(f"expression is not valid JSON: {exc}")
        raise TemplateError(f"unknown method {m!r}")
