# `scripts/placements.json` reference

Every judgement of a mapping run lives in one JSON file, `[run folder]/scripts/placements.json`. The toolkit turns it into the shapefile, evidence CSV, map, README, report and ZIP. No code is written.

Top-level keys (all optional except `parts`):

| Key | Content |
| --- | --- |
| `event` | Event title used in the map, README, report and ZIP name. |
| `refs` | Named geometries reused everywhere (streams, points, outlines, digitized classes). |
| `chainages` | Report stationing (e.g. hm) calibrated on GIS lines. |
| `georef` | Georeferencing of report figures from control points. |
| `visuals` | Ledger of every figure inspected (observation, date/relevance, method). |
| `tables` | Ledger of report tables used. |
| `parts` | One entry per polygon feature of the shapefile. |
| `checks` | Report-vs-GIS comparisons (areas, lengths, distances, chainage, elevations). |
| `data_gaps` | Data searched for but not found: `[{"searched": "...", "result": "...", "effect": "effect on placements"}]`. |
| `report_notes` | Free text for the report: `summary`, `data`, `gaps`, `checks`, `visual`, `sensitivity`, `uncertain`, `other`. |
| `readme_notes` | Extra text for the reviewer README. |
| `map` | `context` layers and `chainage_ticks` for the HTML map. |

Coordinates are in the run CRS (`run.json` → `crs`) unless stated otherwise. `build` warns about every key it does not know (typos are otherwise silently ignored), so read its warnings.

## Geometry expressions

An expression is either a **string** (the name of a `refs` entry) or a **JSON object**. Objects containing `"layer"` select features. Every other object has exactly one operator key. Anywhere inside an expression, the string `"$param"` is replaced by the part's parameter value (see Parameters).

### Feature selection

```json
{"layer": "streams", "where": {"HZB_GEW": "2-8-135-30"}, "merge": "lines"}
```

| Option | Meaning |
| --- | --- |
| `layer` | Local layer name (`inventory` lists them); `digitized:NAME` or `path/file.gpkg:LAYER` also work. |
| `where` | `{"FIELD": value}` equality, or `{"FIELD": {"op": value}}` with op `eq, ne, in, not_in, contains, icontains, startswith, regex, lt, le, gt, ge, isnull`. |
| `ids` + `id_field` | Select by id (default `fid_`, the stable row id shown by `find`/`plot`; use `osm_id` for OSM). |
| `intersects` / `within` / `within_distance` | Spatial filters: an expression, or `[expr, metres]` for `within_distance`. |
| `explode` | Split multi-part features into parts before merging. |
| `merge` | `union` (default), `lines` (one merged line), `first` (error if more than one match), `largest`, `nearest` (with `nearest_to`). |

If nothing matches, the error lists the existing values of the fields you used.

### Lines, points and chainage

| Operator | Example | Result |
| --- | --- | --- |
| `stream` | `{"stream": {"layer": "streams", "where": {...}, "orient": "up", "clip_to": "dem"}}` | Merged line oriented with the DEM so it **starts at its low end (mouth)**. `orient: "down"` reverses it. Lines leaving the DEM are clipped automatically. |
| `start`, `end` | `{"start": "sei"}` | First/last vertex (with `stream`: the mouth / the source). |
| `endpoint_near` | `{"endpoint_near": {"line": "lehn", "to": "mel"}}` | End of `line` nearest to `to` (a tributary mouth). |
| `centroid`, `representative_point` | `{"centroid": "bridge"}` | |
| `nearest_point` | `{"nearest_point": {"on": "sei", "to": "farm"}}` | Point on `on` nearest to `to`. |
| `interpolate` | `{"interpolate": {"line": "l13", "at_m": 120, "origin": "bridge"}}` | Point at a distance along a line (relative to the projection of `origin`; negative values allowed). |
| `substring` | `{"substring": {"line": "mel", "from_m": -50, "to_m": 400, "origin": "mouth"}}` | Part of a line between distances. |
| `between` | `{"between": {"line": "mel", "from": "mouth", "to": "iab_mouth", "extend_m": [0, 50]}}` | Part of a line between the projections of two points. |
| `window` | `{"window": {"line": "l13", "center": "bridge", "length_m": 400, "offset_m": 0}}` | A stretch of given length centred on a point. |
| `chainage_point` | `{"chainage_point": {"chainage": "hm", "at": 18.3}}` | Point at a report station. |
| `reach` | `{"reach": {"chainage": "hm", "from": 6, "to": 32}}` | Line between two report stations. |
| `raster_pixel` | `{"raster_pixel": {"raster": "ortho_2013_2015", "pixel": [1001, 668]}}` | Map point at a pixel (col, row) of a registered raster, e.g. to measure a deck width on an orthophoto. |
| `point`, `point_wgs84`, `line`, `polygon`, `bbox`, `wkt` | `{"point": [66100, 230700]}` | Literal geometry. |
| `file` | `{"file": "tmp/plots/x.geojson"}` or `{"file": {"path": "...gpkg", "layer": "L"}}` | Union of a file's features (e.g. a geometry saved with `probe --save`). |

`chainages` define report stationing on a GIS line:

```json
"chainages": {"hm": {"line": "sei", "unit_m": 100, "anchors": [
  {"value": 0, "at": "start", "label": "mouth"},
  {"value": 18.3, "point": "kal_conf", "label": "Kälberalmbach confluence, report p. 69"}]}}
```

Between anchors the mapping is piecewise-linear (scaled). Beyond the outer anchors it is a constant translation. The report lists each anchor's offset (GIS − report). Anchors are calibration, not validation: check them against another known point with a `chainage` measure.

### Areas

| Operator | Example | Result |
| --- | --- | --- |
| `buffer` | `{"buffer": {"geom": "bridge", "width": 7.4, "cap": "flat"}}` | `width` = full width; or `distance` = one-sided distance. `cap`: `round`, `flat`, `square`. |
| `union`, `intersection`, `difference` | `{"difference": ["dep_outline", "ero70"]}` | Lists of expressions. |
| `half_plane` | `{"half_plane": {"line": {"window": {"line": "l13", "center": "bridge", "length_m": 500}}, "side": "north", "extent_m": 800, "end_extension_m": 0}}` | Area on one side of a line (`north/south/east/west`, or `left/right` of the line direction). **Use a local, roughly straight piece of line** (`window`/`between`): on a long curved road, "north of the whole road" also contains areas you did not mean. Check the result with `plot --refs`. |
| `nearest_part` | `{"nearest_part": {"geom": "ero71", "lines": ["mid", "kal"], "index": 0, "cell_m": 5}}` | Part of `geom` closer to `lines[index]` than to the other lines. Parts for different indices partition `geom` exactly. |
| `dem_mask` | `{"dem_mask": {"within": "catch", "min": 1500, "max": null, "dem": "dem"}}` | DEM cells within an elevation range. |
| `terrain` | see below | Relative height above the nearest channel. |
| `watershed` | `{"watershed": {"pour_point": "mouth", "snap_m": 20}}` | D8 upslope area of a point (priority-flood filled DEM). Use only when no official catchment exists. |
| `largest`, `polygons_only`, `fill_holes`, `convex_hull`, `simplify` | `{"largest": "x"}` | |
| `georef_pixels` | `{"georef_pixels": {"georef": "fig70", "pixels": [[288,512],[328,494],...], "type": "polygon"}}` | Outline digitized in figure pixels and transformed (`type`: `polygon`, `line`, `point`). |
| `georef_color` | `{"georef_color": {"georef": "fig70", "rule": "(R>175)&(G<110)&(B<110)&(R-G>95)", "exclude_px": [[0,0,385,250]], "include_px": null, "min_pixels": 4, "close_px": 0, "open_px": 0}}` | Pixels of a mapped colour class (legend, roofs and other areas excluded with `exclude_px` boxes `[x0,y0,x1,y1]`), transformed to the map. Rule names: `R,G,B` (0–255), `H` (0–360), `S,V` (0–100), plus `abs()`. |

`terrain` (a terrain screening model, **not** hydraulic HAND or a flow simulation):

```json
{"terrain": {"channels": ["fan", {"reach": {"chainage": "hm", "from": 6, "to": 32}}], "max_dist_m": 30,
             "within": "catch", "height_m": "$param", "bed_window_m": 8, "monotone": false, "connected": true, "dem": "dem"}}
```

The bed elevation of each channel sample is the minimum DEM value within `bed_window_m`. `monotone` prevents the bed from rising downstream. Cells within `max_dist_m` of the channels (and inside `within`) are kept when their height above the nearest bed sample is ≤ `height_m` and, with `connected`, when they are 8-connected to the channel.

## Georeferencing (`georef`)

```json
"georef": {"fig70": {
  "figure": "70", "image": "tmp/report_inspection/p18_img2.jpg", "pdf_page": 18, "printed_page": 73,
  "method": "affine", "source_crs": "report orthophoto, CRS not printed",
  "gcps": [{"label": "barn west of mouth", "pixel": [378, 451], "target": {"layer": "osm_buildings", "where": {"osm_id": "w163241721"}, "merge": "first"}},
           {"label": "tick 214000/230000", "pixel": [105, 221], "target": {"source": [214000, 230000], "crs": "EPSG:31257"}},
           {"label": "road bend", "pixel": [674, 243], "target": [66085, 229956]}],
  "checks": [{"label": "withheld house", "pixel": [421, 437], "target": "house_100a"}],
  "limitations": "roof centres vs footprint centroids; ~0.6 m per pixel"}}
```

- `image` is the native figure file listed by `report` (`data_raw/report_images.json`) or a page render. Pixels are native image pixels (origin upper-left, x right, y down). Read them with `render --image FILE --grid 50 --crop ... --zoom 2`, and verify them with `--mark "x,y;x,y"`.
- `target`: map coordinates `[x, y]`; `{"source": [x, y], "crs": "EPSG:…"}` for printed grid coordinates; or any expression (its centroid or point is used).
- `method`: `affine` (≥ 3 GCPs), `similarity` (≥ 2), `projective` (≥ 4). Use more GCPs than the minimum. With exactly the minimum, residuals are zero and say nothing.
- `checks` are withheld points. Their errors are the independent validation. With no check, the report says so.
- `python -m segment_mapping georef RUN` prints RMS, max residual, check errors and pixel size, and writes `tmp/report_inspection/georef_ID_qa.png`: the figure warped onto reference layers with GCP residual vectors.

## Ledgers

```json
"visuals": [{"figure": "70", "evidence_type": "orthophoto", "observation": "what is visible (your words, never a quotation)",
             "date_relevance": "image date and relation to the target event", "method": "how it was used", "caption": "optional exact caption"}],
"tables": [{"table": "12", "printed_page": 79, "observation": "values used"}]
```

Captions are filled automatically from `data_raw/report_captions.json` (or verified if you give `caption`). Pages are taken from the caption when not given.

## Parts

```json
{"part_id": "6c", "seg_id": "6", "part_label": "Mapped fan erosion",
 "geometry": "ero70",
 "parameter": {"name": "height above channel", "unit": "m", "value": 3, "sensitivity": [1.5, 4.5]},
 "placement": "documented", "is_hypo": 0, "confidence": "medium", "rule_5d": false,
 "evidence_type": "orthophoto", "figures": ["70"], "georef": ["fig70"], "tables": ["12"],
 "quotes": [{"printed_page": 70, "start": "Ein kleinerer Teil", "end": "gänzlich zerstört."}, {"text": "exact sentence"}],
 "visual_notes": ["optional extra observation records"],
 "image_date_relevance": "...", "visual_identification_method": "...", "georeferencing_limitations": "(auto if empty)",
 "geom_basis": "≤254 bytes in the shapefile (full text kept in CSV)", "data_src": "layers and figures", "rep_value": "0.65 ha", "rep_page": "70,79",
 "reasoning": "...", "uncertainty": "...", "alternatives": "required when is_hypo = 1",
 "reason_placement": "...", "reason_is_hypo": "...", "reason_confidence": "...",
 "fit_from": ["name of a fitted ref, if the part is cut from it"],
 "checks": [{"label": "...", "measure": {"area_ha": "ero70"}, "report_value": 0.65}]}
```

- `seg_id` must exist in the segment table. Process, category, interaction and phase are copied from it.
- `quotes`: `{"text": …}` or `{"start": …, "end": …}`, with `printed_page` or `pdf_page` as a hint. Matching ignores line breaks, hyphenation and quote/dash variants, and handles page breaks with running headers. Unverified quotations are errors. Test one with `quote RUN --start … --end …`.
- `evidence_type`: `text | table | map | photograph | orthophoto | diagram | mixed`.

### Parameters, fits and sensitivity

- `{"value": v}`: the chosen value replaces `"$param"`.
- `{"fit_area_ha": T, "bounds": [lo, hi], "tolerance_pct": 5}`: bisection on the parameter until the polygon area is within ±5 % of T. Use it only when the report gives an area and no documented outline (rule 5b). The fit is recorded and labelled as not an independent check.
- `"$param"` may also appear inside strings, e.g. a colour rule `"(R>$param)&(G<110)"`. That lets you test digitizing thresholds as a sensitivity.
- `"sensitivity": [lower, higher, ...]`: areas are computed for each value plus the chosen or fitted value.
- **Rule 5d parts** (`"rule_5d": true`) must be `modelled` / `low` / `is_hypo 1`, with `value` and sensitivity values both below and above it.
- Shared fits: put `{"expr": …, "parameter": {"fit_area_ha": 16, …}}` in `refs` (e.g. a combined erosion area), cut parts from it (e.g. `nearest_part`), and list the ref in each part's `fit_from`.

### Classification checks enforced by `build`

- `placement` ∈ documented / approximate / modelled / hypothesis; `confidence` ∈ high / medium / low; `is_hypo` ∈ 0 / 1.
- high ⇒ is_hypo 0.
- Rule 5d ⇒ modelled / low / is_hypo 1, with sensitivity on both sides.
- `documented` outlines are never area-fitted. Without a `figures`/`georef` reference a warning is raised.
- Every segment has ≥ 1 part.
- Required texts: label, basis, sources, reasoning, uncertainty, the three classification reasons, and alternatives for hypotheses.
- Every quotation is verified, and every referenced figure, table and georef exists.

## Checks and measures

```json
"checks": [{"label": "Catchment area", "measure": {"area_km2": "catch"}, "report_value": 4.1, "report_text": "km²", "page": 56, "kind": "comparison", "note": ""}]
```

Each `measure` object has exactly **one** key:

- `area_ha`, `area_km2`, `area_m2`, `length_m`;
- `distance_m: [a, b]`;
- `along_m: {"line", "from", "to"}`;
- `chainage: {"chainage", "geom"}`: report units of the point's projection on the line, plus `lateral_m`. It works for off-line points such as a farm, so no `nearest_point` is needed;
- `chainage_range`;
- `elevation_m: expr`, `dem_stats: expr`;
- `gradient_pct: {"line", "from", "to", "bed_window_m"}`: mean bed gradient between two points along a line;
- `count`, `bounds`, `width_m: {"geom", "length_m"}`.

Use `kind` to say what a check is (`independent`, `calibration`, `fitted`, `comparison`, `ordering`, `derived`, `info`). Differences and % are reported only for comparison-type kinds, not for `ordering`, `derived` or `info`. Try a measure with `probe RUN --measure '{...}'`.

## Map

```json
"map": {"context": [{"geom": "catch", "label": "Official catchment (context)", "color": "#555"}],
        "chainage_ticks": {"chainage": "hm", "values": [0, 3.2, 6, 18.3, 32]}}
```

Base maps come from the preset (`run.json` → `basemaps`), with OpenStreetMap as the fallback.
