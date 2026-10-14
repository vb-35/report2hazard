# Design: text-only localization stage (constrained orchestrator)

Status: **implemented (phases 1-3, CLI)**: `python -m multi_hazard_pipeline localize`. The web review (phase 4) is not implemented yet. Decisions: the stage runs after approval; segments come from the approved run or any provided table (`--segments-table`); packages use the JSON format below; CLI first.

## 1. Goal and assumptions

Place every approved segment of a pipeline run on a map. Use the pipeline's own LLM (chat completions, JSON schema, no tools, no images) inside a fixed orchestration. Python owns the control flow, the geometry and the validation. The model only makes bounded choices in small, schema-checked JSON answers.

Assumptions agreed so far:

- **All spatial data is provided upfront** as a local *spatial package*. There are no downloads, so the stage can run offline.
- **Text-only is the default.** Figures and maps are known only through their captions and the surrounding text, and nothing is georeferenced from images. An image mode can be added later for a vision model.
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
- The manifest gains a `localization` block: status, stages `prepare`, `frame`, `segments`, `build`, `self_evaluation`, `spatial_review`, `spatial_export`, round counters and errors. LLM calls are logged in the existing `timings.jsonl`.

Why after approval: segment IDs and texts are final, so a spatial correction can never ripple into the causal chain. Existing runs and reports without spatial data keep their current workflow.

## 3. Inputs

### 3.1 From the approved run *(exists)*

| Artifact | Used for |
| --- | --- |
| `final_rows.json` | Segment table: segment, event, process, category, interaction, phase. Becomes the toolkit segment table. |
| `segments.json` | Per segment: `evidence[]` with `chunk_id` + exact original-language `quote`, `predecessor_segment_ids`. |
| `translated.json` | Per page chunk: original `text`, `translated_text` (English), `page`, file. Gives the model English text and the toolkit verifiable original quotes with pages. |

### 3.2 Spatial package (new, provided upfront)

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
2. **Report text without the PDF:** write `report_text.json` from `translated.json`, with original text, English text and page. Detect the printed-page offset as today. Captions are parsed from the text, and figures are listed by caption only.
3. Import the package layers and rasters into one copy in the run CRS (`import`), then run `inventory`.
4. Set `run.json` → `mode: "text"`. Validation then forbids `documented` placements, which need an explicit outline the model cannot see, and does not require a visuals ledger.

### L1 Facts and candidates (deterministic, no LLM)

Code computes everything numeric, so the model never does geometry:

- **Name mentions.** For every named feature in the package, find its mentions in the original text. Matching is case- and umlaut-insensitive, accepts ei/ai and similar variants, tolerates line-break hyphenation and inflection suffixes (-s, -es, -e, -er), and allows a small edit distance for long names. Output: `mentions[] = {name, feature_ids, pages, snippet}`. Unmatched capitalised place-like words in evidence quotes become `unresolved_names[]`.
- **Candidate table.** Each candidate is a short id (`S1` stream, `C1` catchment, `B1` bridge, `R1` road, `P1` place, `K1` road-km marker) with a one-line description: name, length or area, the relation to the main candidates, and the pages where it is mentioned.
- **Stream facts.** For each mentioned stream: mouth/source elevations, length, and confluences with other mentioned streams, with distance from the mouth along the receiving stream. Also catchment area, elevation range and highest point.
- **Numeric clues.** hm/km/m/ha/m³/elevation/time expressions with page and snippet (existing `clues` logic).
- **Per-segment evidence bundle.** The segment's approved quotes, plus the clue passages and mentions on the same pages.

### L2 Frame call (1 LLM call per report)

**Input:**
- the English text of the report's location/overview pages (pages with the most mentions, within a size budget);
- the candidate table;
- the stream facts;
- the numeric clues that mention the main stream.

**Output (schema):**

```json
{"main_stream": "S3", "catchment": "C1", "receiving_river": "S7",
 "named_features": [{"report_name": "Lochmure", "candidate": "B2", "basis": "only L13 bridge over S3", "certainty": "inferred"},
                    {"report_name": "Unterhaus", "candidate": "P4", "basis": "...", "certainty": "named"}],
 "unresolved": [{"report_name": "...", "reason": "not in package"}],
 "chainage": {"unit_m": 100, "anchors": [{"value": 0, "point": "mouth"}, {"value": 18.3, "point": "confluence:S5"}]},
 "valley_limits": {"upstream": "confluence:S9", "downstream": "confluence:S11"},
 "quotes": [{"page": 14, "text": "..."}]}
```

**Validation:**
- every id exists, and point tokens are from a fixed grammar (`mouth`, `source`, `confluence:S#`, `feature:B#`, `km:K#`);
- quotations are verified;
- anchors are monotone.

Code then compiles `refs` and `chainages` and computes the calibration offsets. If an anchor's offset is above 10 % of its stationed distance, a repair call asks the model to reconsider, giving it the numbers.

### L3 Segment calls (1 call per segment; small segments batched in threes)

**Input:**
- the segment (process, category, phase, evidence quotes in the original language with English);
- the frame (named refs with one-line descriptions);
- facts (chainage table, reach lengths, catchment area);
- the **method menu** (§5), with the methods allowed for this package;
- condensed rules 5b/5c/5d/6/8 from the prompt.

**Output (schema), one or more parts:**

```json
{"segment": 4, "parts": [
  {"label": "Surge path hm 0-34", "method": "reach_corridor",
   "args": {"stream": "main", "from": {"hm": 0}, "to": {"hm": 34}, "max_dist_m": 80},
   "parameter": {"name": "height_above_channel_m", "value": 8, "justification": "deposits up to 8 m (p. 72)"},
   "report_area_ha": null,
   "placement": "modelled", "is_hypo": 1, "confidence": "low",
   "quotes": [{"page": 59, "text": "Ab etwa 22.20 Uhr ..."}],
   "reasoning": "...", "uncertainty": "...", "alternatives": "...",
   "reason_placement": "...", "reason_is_hypo": "...", "reason_confidence": "..."}]}
```

**Code-enforced consistency, not left to the model:**
- If the report gives no extent for the method (no `report_area_ha` or length), the part is rule 5d: modelled / low / is_hypo 1. Code adds sensitivity values at ×0.5 and ×1.5 of the chosen value, or method-specific steps. The model only justifies the chosen value.
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

- `deliver` writes the map, README, report and ZIP. The map needs offline support (§7).
- In phase 3–4, spatial review is shown on the run page: map and parts table, then approve, reject, or comment per part to trigger an L3 correction.
- Spatial approval writes `localization/outputs/` as the authoritative spatial export, recorded in the manifest.

## 5. Method menu (templates → toolkit expressions)

Each method is a small schema the model fills in. Code compiles it to a `segment_mapping` geometry expression, so the model never writes expressions.

| Method | Args | Compiles to | Classification constraints |
| --- | --- | --- | --- |
| `catchment` | `which: main\|C#` | catchment feature | approximate; high if named and unique |
| `elevation_band` | `min_m`, `max_m`, `within` | `dem_mask` ∩ catchment | modelled; is_hypo 0 only if the report states the elevation |
| `reach_corridor` | `stream`, `from`, `to` (hm / point token), `max_dist_m`, param `height_above_channel_m` | `terrain` on `reach`/`between` | modelled; rule 5d unless the report gives an area (then fitted) |
| `valley_floor` | `river`, `upstream`, `downstream`, `side` (optional: `north/south/left/right` of a road/river), param height | `terrain` on `between` ∩ optional `half_plane` | as above |
| `feature` | candidate id (`B#`, `P#`, building id), param `width_m` for lines | layer selection (+ buffer) | approximate; width without a report value → sensitivity |
| `road_stretch` | road `R#`, centre (point token), `length_m` (report value if given), `use_road_land` or param width | `window` (+ `road_land` ∩) | modelled; length from the report → not 5d |
| `buildings_near` | point token, `radius_m`, optional `max_count` | buildings within a distance | approximate, is_hypo 1, low (proximity ≠ damage; must say so) |
| `point_area` | point token, `radius_m` | buffer around a point | **hypothesis**, is_hypo 1, low (last resort) |
| `expression` | raw toolkit expression | as given | validated like any part; flagged in the report for review |

Point tokens: `mouth`, `source`, `confluence:S#`, `feature:B#`, `km:K#`, `hm:<value>`, `place:P#`. Methods whose role is missing in the package (e.g. no buildings) are left out of the menu, and the gap is reported.

## 6. Prompts and context budget

- **Fixed system prompts** per call type: frame, segment, repair, review. They hold the rule text, the method menu and an example answer. Temperature 0.
- **Size per call:** frame ≈ 30–60 k characters; segment ≈ 10–25 k; repair ≈ 10 k. This fits `max_request_chars = 200000` and keeps each decision small for a 27B model.
- **Candidate lists are ranked:** mentioned features first, then features near the main stream, capped at around 60 per call.
- **Language:** the model reads English translations, but quotations are always the original-language text from `segments.json` or verified against `report_text.json`.
- **Logging:** every request and answer is saved in `localization/llm/` (reproducibility, offline debugging).

## 7. Toolkit changes needed

1. `import-text`: report text, translations and pages from pipeline artifacts (no PDF needed).
2. `import-package` / `package export`: read and write the spatial package manifest (roles, name and id fields, sources).
3. `mode: text` in `run.json`: validation forbids `documented` without figures, needs no visuals ledger, and the report states that no figures were inspected.
4. **Offline map:** Leaflet JS/CSS bundled with the package; base tiles optional (a local hillshade from the DEM as fallback overlay). The map check passes when geometries render without tiles.
5. `facts` module: name mentions (fuzzy), candidate table with short ids, stream network facts, per-segment evidence bundles.
6. `templates` module: method schemas → expressions, with the classification constraints of §5.

## 8. Pipeline changes needed

- `agents/localization_agent.py`: L0–L5 orchestration on top of `ChatClient.complete_json` (existing retries, repair, timing).
- `schemas.py`: frame, segment-parts, repair and review response schemas, generated from the method menu.
- `cli.py`: `localize RUN_DIR --package FILE [--segments 3,4]`. `pipeline.py`: manifest `localization` block and artifact paths.
- Phase 4: `web.py` run page section with a map iframe, parts table, approve/reject and per-part comments.

## 9. Evaluation

| Case | Use |
| --- | --- |
| Seigesbach (approved run `seigesbach-json-escaping-resume__…` + package built from the Sol data) | Development case. Compare with the vision-agent toolkit run and earlier runs: parts per segment, IoU where comparable, classification agreement, rule violations, report-value deviations. |
| A second, held-out report (e.g. Schnannerbach or Vals) | No tuning on it. Checks that the prompts and candidate matching generalise. |

Measures:
- build errors left after repair rounds;
- repair and review calls;
- wall time and tokens;
- share of is_hypo and rule-5d parts;
- unresolved names;
- reviewer acceptance per part.

Text-only results are expected to be weaker on documented outlines, because there are none, and on photo-identified buildings. This should be reported rather than hidden.

## 10. Phases

1. **Toolkit:** `import-text`, spatial package, text mode, offline map, `facts`, `templates`, plus offline tests.
2. **Orchestrator:** L0–L4 as a CLI command on the Seigesbach run, with your model; inspect the output.
3. **Quality:** L5 self-evaluation, manifest integration, second report.
4. **UI:** spatial review and approval in the web interface.

## 11. Decisions needed before implementation

1. **After approval (proposed) or before final approval?** After keeps the existing approval meaning unchanged.
2. **Segment source:** the approved pipeline segments. These differ from hand-curated tables like the Seigesbach prompt's nine segments. Is that what you want to map?
3. **Spatial package:** who prepares it, and is the JSON manifest above acceptable? Should the toolkit export one from a preset download?
4. **Web review in the first version,** or CLI plus map/report files first (proposed)?
