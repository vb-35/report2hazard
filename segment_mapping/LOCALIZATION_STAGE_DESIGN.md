# Design: localization stage (constrained orchestrator)

Status: **implemented (phases 1-3, CLI)**: `python -m multi_hazard_pipeline localize`. The web review (phase 4) is not implemented yet. Decisions: the stage runs after approval; segments come from the approved run or any provided table (`--segments-table`); packages use the JSON format below; CLI first.

## 1. Goal and assumptions

Assess every approved segment for a supported location; retain an explicit unresolved result where evidence is insufficient. Use schema-checked model calls with text and bounded report-image attachments inside a fixed orchestration. Optional declared GIS acquisition and reviewed outline candidates remain controlled by Python. Python owns the control flow, the geometry and the validation. The model only makes bounded choices in small, schema-checked JSON answers.

Assumptions agreed so far:

- **Core GIS inputs are provided** as a local spatial package (catchment, streams, DEM). `--allow-network` optionally enables bounded requests to caller-declared acquisition providers; it does not enable generic web search or invented source URLs.
- **Vision is the CLI default.** Calls receive bounded report-page attachments; `--text-only` disables them. Image inspection does not authorize invented coordinates or pixel measurements. `--figure-manifest` supplies separately reviewed explicit outlines through checked georeferencing.
- **The `segment_mapping` toolkit is the execution engine** *(exists)*. The orchestrator writes `scripts/placements.json` and calls `build` / `deliver`. The same spec can therefore come from this stage or from a vision-capable agent run, and the results compare directly.

## 2. Position in the pipeline

Opt-in, after approval:

```
extraction → translation → segmentation → categorization → candidate → self-evaluation → human review → final export
                                                                                            │
                                                         (approved run) ──► localization ──► spatial review ──► spatial export
```

- Command: `python -m multi_hazard_pipeline localize RUN_DIR --package SPATIAL_PACKAGE.json`. A web button comes in phase 4.
- It runs only on `status == approved` runs and never changes `final_rows.*`, segment text or classifications.
- Everything goes into `RUN_DIR/localization/`, which is a normal `segment_mapping` run folder (`run.json`, `data_raw/`, `scripts/placements.json`, `outputs/`). Orchestrator artifacts go in `localization/llm/`.
- The manifest gains a `localization` block with completion/failure status, counters, review outcome and output paths. Calls/checkpoints are recorded in `localization/llm/`; timings use `llm/timings.jsonl`. Existing localization folders require `--resume`, matching input/settings fingerprints and unchanged sources. Changed inputs require a fresh folder.

Why after approval: segment IDs and texts are final, so a spatial correction can never ripple into the causal chain. Existing runs and reports without spatial data keep their current workflow.

## 3. Inputs

### 3.1 From the approved run *(exists)*

| Artifact | Used for |
| --- | --- |
| `final_rows.json` | Segment table: segment, event, process, category, interaction, phase. Becomes the toolkit segment table. |
| `segments.json` | Per segment: `evidence[]` with `chunk_id` + exact original-language `quote`, `predecessor_segment_ids`. |
| `translated.json` | Per page chunk: original `text`, `translated_text` (English), `page`, file. Gives the model English text and the toolkit verifiable original quotes with pages. |

### 3.2 Spatial package

One JSON manifest per area. It declares roles and name fields, so the deterministic steps work in any region without guessing field names:

```json
{
  "name": "Sellrain / Seigesbach",
  "crs": "EPSG:31254",
  "layers": [
    {"name": "catchment",  "path": "catchment.gpkg", "role": "catchment", "name_field": "NAMEEZGWLV", "id_field": "HZBEZGWLV"},
    {"name": "streams",    "path": "streams.gpkg",   "role": "streams",   "name_field": "GEW_NAME_HAUPT", "id_field": "HZB_GEW"},
    {"name": "roads",      "path": "roads.gpkg",     "role": "roads",     "name_field": "NAME", "ref_field": "STR_CODE"},
    {"name": "road_km",    "path": "road_km.gpkg",   "role": "road_km",   "value_field": "KM1", "ref_field": "STR_CODE"},
    {"name": "bridges",    "path": "bridges.gpkg",   "role": "bridges",   "name_field": "NAME"},
    {"name": "buildings",  "path": "buildings.gpkg", "role": "buildings"},
    {"name": "names",      "path": "places.gpkg",    "role": "names",     "name_field": "NAME"},
    {"name": "road_land",  "path": "parcels.gpkg",   "role": "road_land"}
  ],
  "rasters": [{"name": "dem", "path": "dem_5m.tif", "role": "dem"}],
  "sources": [{"name": "streams", "provider": "Land Tirol", "licence": "CC BY 4.0", "date": "2024"}]
}
```

- **Required roles:** `catchment`, `streams`, `dem`.
- **Recommended roles:** roads, bridges, buildings, names, road_km, road_land.
- **Missing roles** become `data_gaps` automatically and limit which methods are allowed.
- **Helper:** the toolkit's `fetch preset` can build such a package once while online (`package export`, phase 1).

## 4. Stages

### L0 Prepare (deterministic)

1. Create `localization/` with `init`, using the segment table from `final_rows.json`.
2. Write `report_text.json` from `translated.json`, retaining original/English text and page provenance. In vision mode, render selected source PDF pages separately and record attachments/uninspected pages and failures; text-page IDs can differ from original file pages.
3. Import the package layers and rasters into one copy in the run CRS (`import`), then run `inventory`.
4. Record actual image inspection mode and visual observations. Verified outline candidates retain independent alignment and event-interpretation evidence. Without usable image/outline evidence, do not claim a documented boundary.

### L1 Facts and candidates (deterministic, no LLM)

Code computes everything numeric, so the model never does geometry:

- **Name mentions.** For every named feature in the package, find its mentions in the original text. Matching is case- and umlaut-insensitive, accepts ei/ai and similar variants, tolerates line-break hyphenation and inflection suffixes (-s, -es, -e, -er), and allows a small edit distance for long names. Output: `mentions[] = {name, feature_ids, pages, snippet}`. Unmatched capitalised place-like words in evidence quotes become `unresolved_names[]`.
- **Candidate table.** Each candidate is a short id (`S1` stream, `C1` catchment, `B1` bridge, `R1` road, `P1` place, `K1` road-km marker) with a one-line description: name, length or area, the relation to the main candidates, and the pages where it is mentioned.
- **Stream facts.** For each mentioned stream: mouth/source elevations, length, and confluences with other mentioned streams, with distance from the mouth along the receiving stream. Also catchment area, elevation range and highest point.
- **Numeric clues.** hm/km/m/ha/m³/elevation/time expressions with page and snippet (existing `clues` logic).
- **Per-segment evidence bundle.** The segment's approved and linked quotes, plus the clue passages and mentions on the same pages (up to 4 pages).
- **Deduplication.** The same feature supplied by several layers becomes one candidate with aliases. Bridges record the stream they cross, its station and whether it is the only bridge there, and the road they carry.

### L1b Evidence linking (LLM, batched pages)

Approved evidence says what happened; the statements that locate it (stations, areas, named limits) are often on other
pages, and a segment table has no evidence at all. Batched calls (about 40 k characters of pages each, without images)
return up to 6 passages per segment and batch, each with page, exact original-language quote, kind
(place/station/extent/quantity/timing) and note. Code verifies every quote and drops unverifiable ones; a batch with
many unverifiable quotes is repaired. The linked passages feed the frame page choice, the segment calls and the review.
Incidents the report lists as further reports, at other torrents or elsewhere in the municipality are not linked.

### L2 Frame call (1 LLM call per report)

**Input:**
- the English text of the report's location/overview pages (pages with the most mentions, within a size budget);
- the candidate table;
- the stream facts;
- the numeric clues that mention the main stream.

**Output:** `main_stream`, `catchment`, optional `receiving_river`, `named_features` (report name, candidate, certainty, basis, page), `chainage_anchors` (value, point, page, quote), `report_numbers` and `notes`. Current strict response schemas are generated in `agents/localization_agent.py`; candidate IDs come only from computed facts.

**Validation:**
- every id exists, and point tokens are from a fixed grammar (`mouth`, `source`, `confluence:S#`, `feature:B#`, `km:K#`);
- quotations are verified;
- anchors are monotone.

Code then compiles `refs` and `chainages` and computes the calibration offsets. If an anchor's offset exceeds max(150 m, 20% of its stationed distance), a repair call asks the model to reconsider, giving it the numbers.

### L3 Segment calls (one call per segment)

**Input:**
- the segment (process, category, phase, evidence quotes in the original language with English);
- the frame (named refs with one-line descriptions);
- facts (chainage table, reach lengths, catchment area);
- the **method menu** (§5), with the methods allowed for this package;
- condensed rules 5b/5c/5d/6/8 from the prompt.

**Output:** original `segment` ID, `localization_status`, `reason`, independent `event_confidence`, `location_confidence`, `boundary_confidence`, `data_requests`, and zero to four `parts`. Unresolved results contain no parts and unknown location/boundary confidence. Each located part carries its method, args, role, independent confidences, parameter evidence, original-language quotes and classification reasons. See [SPEC.md](SPEC.md#outcomes-and-exports) for compiled outcomes and [README.md](README.md#optional-public-data-acquisition) for acquisition/outline manifest examples.

**Code-enforced consistency, not left to the model:**
- Assumed reconstruction parameters trigger rule 5d: modelled / low / is_hypo 1; a supported native location needs no assumed width. Code adds sensitivity values at ×0.5 and ×1.5 of the chosen value, or method-specific steps. The model only justifies the chosen value.
- With `report_area_ha`, a terrain method is fitted to the area (±5 %), and the fit is labelled as not independent.
- `confidence: high` requires `is_hypo: 0` and a frame feature with `certainty: named`.
- A part's quotes must come from the segment's evidence bundle or be verifiable in the report.

### L4 Build and repair (deterministic + targeted calls)

1. Compile all parts into `placements.json` through the method templates (§5).
2. Run `build`.
3. Map each build error to its part and send a repair call to that segment only, with the error text, the previous answer and the computed geometry summary (area, empty or not, bounds relative to the catchment).
4. At most 2 rounds, matching `max_correction_rounds`. Remaining errors stop the stage with a clear status. Nothing is silently dropped.

### L5 Self-evaluation (1 call, optional at first)

A reviewer prompt gets the compiled parts table: method, args, classification, areas, quotes and checks. It returns structured issues per part (classification vs rule 8, proximity used as damage, unsupported widths, segment without plausible placement). Each issue triggers a targeted L3 re-run, bounded like the existing correction loop. This mirrors `review_agent`.

### L6 Deliver and spatial review

- `deliver` writes canonical mixed `segments.geojson`, an optional polygon-only shapefile, complete result JSON/CSV, evidence, map, README, report and ZIP. Points/lines retain native geometry and null area. Leaflet/data are embedded; web tiles are optional. Entirely unresolved runs remain exportable.
- The local review interface can launch/resume localization for each approved report, monitor its activity and diagnostics, and show the map, segment outcomes and downloads. Browser-based per-part spatial approval/correction remains future work.
- Completion distinguishes `delivered`, `delivered_with_unresolved`, `partial`, `incomplete`, `review_incomplete`, `revision_required`, `build_errors`, `validation_errors`, `failed`. Outstanding issues/failures cannot become ordinary delivered success. Web spatial approval remains future work.

## 5. Method menu (templates → toolkit expressions)

Each method is a small schema the model fills in. Code compiles it to a `segment_mapping` geometry expression, so the model never writes expressions.

| Method | Args | Compiles to | Classification constraints |
| --- | --- | --- | --- |
| `catchment` | `candidate: catch\|C#` | catchment feature | approximate; high if named and unique |
| `elevation_band` | `candidate`, `min_elev_m`, `max_elev_m` | DEM mask within catchment | modelled; report evidence must support the elevation constraint |
| `reach_corridor` | `stream`, `from_point`, `to_point`, `max_dist_m`; parameter height | terrain band on a reach | modelled; assumed height triggers rule 5d; area comparison unless explicitly fitted |
| `valley_floor` | `river`, `upstream`, `downstream`, `side` (optional: `north/south/left/right` of a road/river), param height | `terrain` on `between` ∩ optional `half_plane` | as above |
| `feature` | candidate id (`B#`, `P#`, building id), param `width_m` for lines | layer selection (+ buffer) | approximate; width without a report value → sensitivity |
| `road_stretch` | road `candidate`, `center`, `length_m`, `use_road_land`; parameter width | road window / land parcels / buffered line | modelled; assumed width triggers rule 5d |
| `buildings_near` | `center`, `radius_m` | nearby building footprints | is_hypo 1; proximity alone does not prove damage; only when the report identifies the building |
| `named_area` | `center`, `radius_m` (50-500 m) | buffer around a named place | approximate, always `search_area`, unknown boundary: for "in the area of X", hamlets, farm groups |
| `point_area` | `center`, parameter radius | buffer around a point | **hypothesis**, `search_area`, is_hypo 1, low |
| `location_point` | `center` point token | native point | `location`; unknown boundary confidence |
| `channel_reach` | `stream`, `from_point`, `to_point` | native line reach | `location`; no invented width |
| `feature_geometry` | `candidate` | native GIS geometry | `location`; feature identity does not establish damage extent |
| `report_outline` | reviewed candidate ID linked to this segment | preserved verified polygon | documented only after alignment and event/date review |

Point tokens: `mouth`, `source`, `confluence:S#`, `feature:B#`, `km:K#`, `hm:<value>`, `place:P#`. `expression` remains a legacy toolkit template but is not offered to the localization model. Methods whose role is missing in the package (e.g. no buildings) are left out of the menu, and the gap is reported.

## 6. Prompts and context budget

- **Fixed system prompts** per call type: frame, segment, repair, review. They hold the rule text, the method menu and an example answer. Temperature 0.
- **Size per call:** frame ≈ 30–60 k characters; segment ≈ 10–25 k; repair ≈ 10 k. This fits `max_request_chars = 200000` and keeps each decision small for a 27B model.
- **Candidate lists are ranked:** mentioned features first, then features near the main stream, capped at around 60 per call.
- **Language:** the model reads English translations, but quotations are always the original-language text from `segments.json` or verified against `report_text.json`.
- **Logging:** every request and answer is saved in `localization/llm/` (reproducibility, offline debugging).

## 7. Toolkit support

1. `import-text`: report text, translations and pages from pipeline artifacts (no PDF needed).
2. `import-package` / `package export`: read and write the spatial package manifest (roles, name and id fields, sources).
3. `mode: text` records absent report-image inspection; documented outlines require verified figure evidence rather than inferred photo boundaries.
4. **Offline map:** Leaflet JS/CSS bundled with the package; base tiles optional (a local hillshade from the DEM as fallback overlay). The map check passes when geometries render without tiles.
5. `facts` module: name mentions (fuzzy), candidate table with short ids, stream network facts, per-segment evidence bundles.
6. `templates` module: method schemas → expressions, with the classification constraints of §5.

## 8. Pipeline implementation

- `agents/localization_agent.py`: L0–L5 orchestration on top of `ChatClient.complete_json` (existing retries, repair, timing).
- `schemas.py`: frame, segment-parts, repair and review response schemas, generated from the method menu.
- `cli.py`: `localize RUN_DIR --package FILE [--only 3,4] [--resume] [--allow-network] [--figure-manifest FILE]`. `pipeline.py`: manifest `localization` block and artifact paths.
- Phase 4: `web.py` run page section with a map iframe, parts table, approve/reject and per-part comments.

## 9. Evaluation

| Case | Use |
| --- | --- |
| Seigesbach (approved run `seigesbach-json-escaping-resume__…` + package built from the Sol data) | Development case. Compare with the vision-agent toolkit run and earlier runs: parts per segment, IoU where comparable, classification agreement, rule violations, report-value deviations. |
| A second, held-out report (e.g. Schnannerbach or Vals) | No tuning on it. Checks that the prompts and candidate matching generalise. |

`segment_mapping.benchmark` scores a run against reference runs (frontier-model runs treated as approximately correct):
per segment precision, recall against the majority area, site recall, IoU, area ratio with a scale penalty, and a
leave-one-out ceiling between the references. Prompt examples must not come from the benchmark report.
Search areas (`named_area`, `point_area`) state where, not how large: they count for recall and site recall, and for
precision as the share that holds a reference place; IoU and the area ratio use the extent parts only. This keeps
reference runs that guess individual damaged buildings from rewarding false precision.

Measures:
- benchmark score per segment and report;
- precision tiers (specific/approximate/broad/unresolved);
- build errors left after repair rounds;
- repair and review calls;
- wall time and tokens;
- share of is_hypo and rule-5d parts;
- unresolved names;
- reviewer acceptance per part.

Text-only results lack direct image interpretation. Coverage is assessed by supported locations and unresolved outcomes, not polygon count. The Seigesbach Abb. 71 pilot remains unresolved because printed CRS and event/date interpretation are unverified and the legend classes conflict; no verified Seigesbach affected-area result is claimed.

## 10. Phases

1. **Toolkit:** `import-text`, spatial package, text mode, offline map, `facts`, `templates`, plus offline tests.
2. **Orchestrator:** L0–L4 as a CLI command on the Seigesbach run, with your model; inspect the output.
3. **Quality:** L5 self-evaluation, manifest integration, second report.
4. **UI:** spatial review and approval in the web interface.

## 11. Established scope

The CLI runs after approval, preserves approved extraction artifacts, accepts an optional external segment table, and uses declared spatial packages. Browser-based spatial approval remains future work. Acquisition and reviewed figures are opt-in inputs; uncertain spatial evidence stays unresolved.
