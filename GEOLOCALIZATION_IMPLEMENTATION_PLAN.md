# Implementation plan for localizing report segments as geographic areas

## Questions to resolve

These questions affect implementation and study design. The proposed defaults below make the plan actionable; they are assumptions for planning, not decisions already made by the user.

1. **Which region and reports should form the pilot?** Proposed default: one region and approximately 20 reports covering different levels of geographic detail, with roughly 100 expert-annotated segments. Confirm the sample after inspecting available material.
2. **Which DEM and reference layers are available, and may they be redistributed with results?** Needed: coverage, resolution, coordinate reference system, vertical datum, acquisition dates, terrain versus surface model, license, river network, local place names, and any event footprints. Proposed default: user-supplied local files; no automatic external downloads.
3. **Does “area” mean the observed event footprint, a named geographic unit, or an area within which the event probably occurred?** Proposed default: support all three meanings and label them separately. Never present a catchment or uncertainty envelope as an observed footprint.
4. **What spatial detail does the study require?** Catchment, slope unit, channel corridor, deposit polygon, or infrastructure impact area? Proposed default: the most specific area supported by the evidence; retain broader or unresolved results when necessary.
5. **Are useful maps already georeferenced, or are they figures embedded in PDF or DOCX reports?** Proposed default: start with georeferenced layers and manually registered report maps. Include document figure extraction and assisted interpretation in a subsequent milestone.
6. **Must the LLM run locally, and does the configured service support images?** everything is local. no images supported by LLM api
7. **May unresolved or approximate areas appear in approved results?** Proposed default: yes, with an explicit reviewer acknowledgement and visible limitations. Missing scientific evidence must not force an invented polygon.
8. **Who can review locations and create reference annotations?** Proposed default: a GIS or hazard-domain reviewer, with a second reviewer adjudicating the evaluation sample. Human approval is already part of this project.
9. **Which outputs are required downstream?** Proposed default: approved GeoJSON polygons, a spatial CSV summary, and JSON provenance; retain the existing six-column classification CSV. Add GeoPackage export only if a downstream workflow requires it.
10. **How much manual preparation and correction is acceptable?** Proposed default: prepare an initial area catalog in QGIS and allow reviewers to replace polygons through GeoJSON import. Automatic map registration and browser polygon drawing are separate follow-on tasks.
11. **Should geography become mandatory for every new report?** Proposed default: opt in per run. Existing runs and reports without spatial packages keep their current workflow. Spatially enabled runs require spatial review before combined approval.
12. **What accuracy and review-time improvement would justify adoption?** Proposed default: measure these in the pilot and agree thresholds before evaluating a held-out set. This plan does not claim an accuracy or speed improvement in advance.

## Objective and completion criteria

Extend the existing multi-hazard report pipeline so each causal segment receives a traceable geographic interpretation. The output can contain one or several polygons, alternative candidate areas, or an explicit unresolved or not-applicable result. Areas must be linked to original report evidence, supplied maps, and the spatial data used to construct them.

The LLM interprets geographic language and selects or requests bounded operations on known spatial features. GIS code owns coordinates, polygon construction, terrain measurements, and geometric validation. A DEM supplies terrain context; it does not prove a historical event footprint.

The first usable release includes local spatial-package registration, candidate-area generation from prepared features, DEM summaries, structured localization, spatial review, correction handling, and approved exports. Document imagery and automated terrain-unit preparation are additional milestones where the pilot inputs require them. Full autonomous reconstruction of every event boundary is a research outcome to evaluate, not a prerequisite that can be promised.

Completion requires an end-to-end spatial run, a functioning correction and approval workflow, regression tests for ordinary runs, and an evaluated pilot. Every segment must have a geographic record; every segment does not have to have a polygon.

## Existing project behavior and integration points

The current workflow is extraction, translation, segmentation, categorization, candidate construction, self-evaluation and correction, human review, and final export. It handles companion PDF, DOCX, and TXT files as one logical report, with a separate path for splitting PDF collections into independent event reports.

The project already has stable segment IDs, original-text citations, bilingual source context, controlled classifications, run manifests, atomic writes for individual JSON and CSV files, a per-run lock, and a single background worker. Reuse these mechanisms.

Geographic fields are absent from the segment schemas. `agents/source_agent.py` extracts PDF text and skips pages without extractable text; it does not supply OCR or map geometry. `llm.py` sends text JSON to a chat-completions service and does not currently send images. `human_review.py` and the existing report evaluator route corrections through translation, segmentation, and categorization only. The download allowlist contains three report artifacts. Spatial changes therefore need integration across the whole lifecycle.

Use a spatial sidecar linked by run ID and segment ID. Keep the report taxonomy and six-column classification export intact. The report reviewer continues to evaluate the causal chain; spatial validation has its own artifact and participates in the final approval gate.

## Proposed workflow

1. Select the logical report and explicitly attach a registered spatial package and study boundary.
2. Extract, translate, segment, classify, and finish the existing report self-evaluation and correction process.
3. Extract geographic assertions from the corrected segments and relevant original and translated report context.
4. Retrieve local reference features and generate candidate polygons using permitted GIS operations.
5. Ask the LLM to select candidates and explain their geographic meaning and evidential basis.
6. Validate the localization records, citations, geometry, package references, and revision dependencies.
7. Present the causal chain and area map together for human review. Permit replacement, correction, or acceptance of an unresolved result.
8. Publish report and spatial exports together only after their current revisions meet the approval rules.

Running localization after textual corrections prevents repeated GIS work for intermediate causal chains. Subsequent report edits must invalidate or refresh the affected geography before approval. Implement one shared spatial refresh and readiness function called by initial execution, human edits, and correction completion.

## Spatial inputs and package registration

Represent a spatial package with a versioned `package.json`, a study boundary, a DEM GeoTIFF, and reference GeoJSON files. Register packages below `results/geodata/<package_id>/`. Keep imported source files immutable; derive a package revision from their checksums and metadata. Share those immutable assets across runs and record the exact package revision in each run.

The package manifest must identify each layer by a stable layer ID and record its relative path, checksum, source, license, original CRS, date or date range, and intended use. DEM metadata must also include horizontal and vertical units, vertical datum if known, pixel spacing, NoData value, terrain or surface model designation, and coverage. Unknown metadata remains explicitly unknown.

Require a study boundary and at least one usable area layer for the first release. Useful area layers include catchments, slope units, fans, mapped deposits, administrative units, and hazard inventory footprints. Lines and points can supply anchors such as rivers, roads, bridges, and villages; their presence alone does not define an event area.

Area-catalog entries require a stable feature ID, name and language aliases, feature type, geometry, source-layer reference, applicable dates, and preparation method. Precomputed DEM statistics are derived metadata. Retain event date and map acquisition date separately.

Initially accept GeoTIFF and GeoJSON. Prepare other vector formats in QGIS rather than adding format handlers before they are needed. Import large DEMs through a local registration command instead of the existing report upload form, which has a 100 MB request limit. The browser chooses a registered package ID; it must not submit unrestricted filesystem paths.

During registration validate paths, checksums, CRS, finite coordinates, geometry types, unique feature IDs, study-area overlap, DEM readability, and usable raster coverage. Reject invalid inputs before modifying a run. Warn about incomplete coverage, missing vertical datum, incompatible dates, or terrain detail insufficient for the requested feature size. Do not infer an unknown CRS from the apparent coordinate values.

Clip and summarize rasters with bounded window reads. Proposed resource limits must be made explicit during the Windows installation spike: maximum raster window size, catalog feature count, polygon vertices, and map preview dimensions. An exceeded limit produces an actionable error or a smaller requested study area, not silent feature omission.

## Coordinates and terrain preparation

Choose a suitable projected analysis CRS in metres for each package and record it. Transform vectors and raster data consistently for slope, distances, buffers, and area calculations. Transform exported GeoJSON to WGS84 longitude and latitude. Use explicit axis order and test against a known location; Leaflet latitude-longitude arguments differ from GeoJSON coordinate order.

Retain original geometry alongside prepared geometry where reprojection or digitization occurred. Verify transformations and any necessary datum grids during package registration. Do not claim a vertical-datum conversion has occurred when the requisite information or grids are absent.

For each candidate area compute bounded summaries: area, elevation range and selected percentiles, slope distribution, aspect where meaningful, DEM coverage fraction, and relationships to known channels or anchors. Treat aspect as circular data and exclude undefined aspect on flat terrain. Use raster masks and document boundary-cell treatment.

Create slope and aspect with established terrain algorithms, recording their parameters and edge handling. For the pilot, prepare drainage directions, catchments, and slope units with QGIS or GRASS and import the results. If repeated manual preparation becomes a bottleneck, add a narrowly scoped GRASS command integration that records the executable version, preprocessing, outlet snapping, and command parameters. Do not implement a new flow-routing algorithm.

Hydrological preparation must address depressions, artificial barriers, DEM edges, and stream-network alignment. A catchment generated from an outlet represents upstream drainage, not observed inundation. Channel corridors require mapped widths, explicit report widths, or a documented approximation accepted by the reviewer. Never add a universal buffer radius merely to turn a point or line into an area.

Copernicus DEM is a surface model including vegetation and structures; the official collection includes GLO-30 at approximately 30 m resolution. If used, its suitability for small channels and scars must be evaluated rather than assumed. [Copernicus DEM documentation](https://dataspace.copernicus.eu/explore-data/data-collections/copernicus-contributing-missions/collections-description/COP-DEM).

## Geographic assertion extraction and candidate retrieval

Add a structured localization agent that first extracts assertions: place names, feature types, elevation constraints, distances with units, directions, upstream or downstream relationships, temporal qualifiers, negation, and ambiguity. Every assertion must cite an original source chunk and literal or whitespace-normalized quotation. Keep the original place spelling and multilingual aliases alongside English interpretation.

Supply relevant contextual chunks as well as each segment's existing evidence. Geographic anchors may appear in a heading or an earlier paragraph. Record which context establishes the location and which establishes the process. Avoid carrying a report-level location into every segment without evidential support.

Resolve names against the local catalog, first using normalized names and known aliases, then bounded approximate matching if the pilot demonstrates a need. Return multiple candidates for ambiguous names. Distinguish “the eastern slope above the village” from “an east-facing slope”; relative position and terrain aspect are different constraints. Preserve qualifiers such as “possibly” and “approximately.”

Construct or retrieve candidates with a small set of explicit operations: select a known area, select a bounded channel reach, intersect a known terrain unit with an evidenced elevation interval, or retrieve a catchment associated with a verified outlet. References and numbers must resolve to validated package data or cited evidence. GIS code executes operations; model output must never become arbitrary Python, SQL, or shell code.

Proposed initial limit: provide at most 25 candidate summaries per segment. If more plausible candidates remain, request more context, narrow the retrieval using supported constraints, or record ambiguity. Preserve that retrieval was limited; do not treat absence from the shortlist as evidence against an area.

Return candidate IDs, names, feature types, source descriptions, terrain summaries, and relationships to anchors. Keep authoritative coordinates outside the text prompt. Retain the full geometry locally and simplify only the map display, with a recorded display tolerance.

The second model step chooses candidates and returns geographic roles, evidence, boundary meaning, rejected alternatives, and unresolved reasons. It can choose several complementary areas or retain several competing interpretations. These cases must be represented differently.

Reuse `ChatClient.complete_json`, strict response schemas, validation retries, timing records, and the serialized request ceiling. Batch by segment and context size. Never submit an entire DEM as prompt text. Do not label model confidence as a calibrated probability; retain a qualitative assessment tied to evidence and measure reliability on annotated data.

## Data contracts and run artifacts

Add spatial schemas to `schemas.py` while keeping existing report schemas compatible. Use `schema_version: 1` for new spatial artifacts and reject unsupported versions with a migration message.

| Artifact | Required content |
| --- | --- |
| `spatial_context.json` | Package ID and revision, source checksums, study boundary reference, analysis CRS, dates, processing configuration, and available layer IDs |
| `spatial_candidates.geojson` | Candidate polygons with stable area IDs, feature and layer references, geographic roles, derivation parameters, and terrain summaries |
| `localization.json` | Exactly one record per current segment, assertions, selected area IDs, alternatives, evidence, meaning, status, limitations, and review metadata |
| `spatial_evaluation.json` | Evaluated revisions and hashes, structural checks, geographic warnings, blocking issues, and evaluation history |
| `final_areas.geojson` | Approved polygon features with run ID, segment ID, area ID, role, boundary meaning, evidential basis, revision, and source references |
| `final_localization.json` | Approved full localization records, including unresolved segments and provenance |
| `final_spatial_summary.csv` | One row per segment-area assignment; one row with blank area fields for segments without geometry |

Each localization record needs `segment`, `segment_content_hash`, `status`, `selected_areas`, `alternative_groups`, `assertions`, `evidence`, `unresolved_reason`, and `limitations`. Hash the event, process, evidence, classification, causal relationships, and relevant source context; stable IDs alone cannot establish freshness.

Use localization statuses `resolved`, `ambiguous`, `unresolved`, and `not_applicable`. An operational failure belongs in run spatial status, not in a scientific unresolved result. Use separate boundary meanings `process_footprint`, `feature_extent`, and `location_uncertainty`. Use evidential bases `documented`, `terrain_inferred`, and `expert_interpreted`, with supporting source references. A clearly located feature can still have an uncertain event boundary.

Each selected area assignment needs an area ID, role such as source, transport, deposition, trigger extent, blockage, or impact, boundary meaning, evidential basis, and citations. A segment can reference multiple polygons and overlapping roles. A MultiPolygon can represent disconnected parts of one area. Alternative groups contain competing interpretations and must never be exported as if all alternatives occurred.

Map evidence needs source document ID, original page or figure reference, map asset checksum, marked region reference, and georeferencing metadata. Original-text evidence uses the existing chunk citation mechanism. Human replacement geometry requires its own derivation and review record.

Keep localized records authoritative in the spatial sidecar, joining them to candidate rows in the UI. Add a spatial revision reference to enabled-run candidate metadata. Keep the existing `final_rows.csv` columns; link spatial outputs using run ID and segment ID. GeoJSON includes only approved selected geometry. Unresolved records remain discoverable in JSON and CSV even when there is no map feature.

## Spatial validation and review policy

Deterministic checks must cover one record per current segment, valid segment and candidate IDs, citation existence, allowed roles and statuses, finite coordinates, nonempty Polygon or MultiPolygon geometry, positive area, valid topology, and valid package references. Detect self-intersections, coordinate swaps, geometry outside the study boundary, duplicate assignments, stale hashes, and insufficient DEM coverage.

Reject invalid geometry or expose a proposed repair for review. Do not silently repair a polygon if doing so changes its interpretation or extent. Require a recorded tolerance and acknowledgement for a legitimate boundary crossing rather than clipping an area silently.

Use terrain and connectivity checks as contextual checks, with clear limits. A transport reach inconsistent with the supplied drainage network should generate a warning or issue. Causal predecessor relationships do not imply that polygons must touch, nor that every process is governed by surface water routing. A numerical elevation or distance constraint can be checked automatically; whether the report identifies the correct slope remains a semantic judgment.

The spatial evaluator produces `pass` or `revision_required` for machine validation, accompanied by warnings and evaluated hashes. Missing evidence can be a valid unresolved outcome. Human review remains required for resolved, ambiguous, and unresolved records before combined approval. Accepting an unresolved result documents the limitation; it does not create geometry.

Spatially enabled approval requires a passing current report evaluation, a passing current spatial evaluation, matching report and package revisions, and reviewer acknowledgement of all current segment localizations. Legacy runs without a spatial context use the existing approval rule. A corrupt or missing artifact on a spatially enabled run blocks approval rather than downgrading the run to text-only.

## Corrections and revision invalidation

Implement the refresh rule in one shared backend path, used after initial text evaluation, `_apply_candidate_edits_unlocked`, and `_request_correction_unlocked`. Add a dedicated localization correction branch; do not send it through `correct_until_terminal`'s current categorization fallback or the report review schema, whose stage enum excludes localization.

| Change | Required spatial action |
| --- | --- |
| Translation, event, process, evidence, or segmentation | Mark spatial results stale and rebuild affected assertions and localizations after report validation |
| Segment insertion, deletion, or merge | Reconcile coverage; archive orphaned records; require review of replacement assignments |
| Classification or causal relationship | Re-evaluate geographic roles and applicable constraints; invalidate review acknowledgements |
| Order change only | Preserve geometry where content is unchanged; refresh order links and revision checks |
| Package, study boundary, or processing parameters | Rebuild affected candidates and localization; retain previous revisions for audit |
| Human candidate selection or replacement polygon | Validate geometry and evidence; increment spatial revision; rerun spatial checks |

For the first release, recompute localization for the whole bounded report after material text or package changes. Reuse immutable package preparation. This is simpler than dependency-based partial GIS invalidation and must be recorded as a deliberate performance ceiling; add targeted recomputation only if measured latency requires it.

Block approval as soon as work is queued, using the existing run lock and queue pattern. Mutating operations accept the revision observed by the reviewer and reject stale submissions. Keep previous spatial revisions before replacement so automated refresh cannot silently erase a human correction. Human acknowledgements apply only to the exact spatial and report content reviewed.

Localization-only automatic correction requests count toward the existing run-wide correction limit. Input registration and manual geometry correction do not consume model correction rounds. Manual validated edits remain possible after automatic rounds are exhausted, matching existing behavior.

## User interface and command line

Add a spatial-package selector and explicit localization option to report creation. For PDF collections, attach geography per child report. A shared regional package can be offered to all children only when its coverage and intended use are confirmed; event-specific maps must keep their child association and original page provenance.

Add an interactive area map to `templates/run.html` and a small `static/map.js`. Use locally served Leaflet assets and a locally prepared hillshade or registered map preview, so map review does not depend on a public tile service. GeoJSON supplies polygon overlays. Provide attribution for every displayed source.

Selecting a segment highlights its areas; selecting an area opens the segment, quotations, and map provenance. Use separate visual styles for documented footprints, feature extents, inferred areas, uncertainty envelopes, and alternatives. Include a legend and a text table so color and map interaction are not the only access paths. Show absent geometry, limitations, revision state, and processing failures explicitly.

Reviewers can choose a candidate, reject an assignment, request localization correction, or acknowledge an unresolved result. Support importing replacement Polygon or MultiPolygon GeoJSON prepared in QGIS. Browser vertex drawing can be added later if import-based correction proves cumbersome; it is not necessary to evaluate localization quality.

Serve map assets and candidate geometry through allowlisted routes scoped to the selected run or registered package. Escape labels and quotations in popups. Keep authoritative downloads separate from candidate previews and guard all `final_*` downloads, including spatial files.

Proposed CLI additions, to be implemented:

```powershell
python -m multi_hazard_pipeline spatial-import PACKAGE_DIR
python -m multi_hazard_pipeline run INPUT_DIR results --spatial-package PACKAGE_ID
python -m multi_hazard_pipeline localize RUN_ARTIFACT_DIR --spatial-package PACKAGE_ID
python -m multi_hazard_pipeline correct RUN_ARTIFACT_DIR --stage localization --segments 2,4 --comment "Use the upstream reach"
```

The localize command attaches or refreshes geography on a reviewable run after text evaluation passes. For an already approved historical run, create an explicitly linked new revision or derivative run requiring review; do not mutate its authoritative export in place. Update the CLI legacy-command recognition set and preserve existing exit codes.

## Failures and export publication

Add a spatial substate to the manifest: disabled, pending, running, ready, revision_required, or failed. Preserve existing top-level states. Disabled localization makes no spatial model calls and needs no GIS dependencies at import time.

An explicitly enabled run with invalid spatial inputs, missing GIS dependencies, or an exhausted localization request records a spatial failure and remains unapprovable until repaired or explicitly changed to a new text-only revision. Preserve the successfully extracted report and expose diagnostics. Do not convert failed processing into scientific uncertainty or hide it behind an empty map.

Reuse the single worker for expensive GIS and LLM operations. Bound raster memory, serialize per-run mutations, and record stage timing. Since the current worker is in memory, a restart can leave pending work unfinished; display the interrupted state and offer an explicit spatial rerun. Use revision hashes to avoid publishing outputs from an interrupted older task.

Extend approval staging to all final artifacts. Write temporary files in the destination directory, validate them, then publish under the run lock with the approved manifest as the final commit marker. Multi-file replacement is not inherently atomic: readers must require the committed manifest and matching export revision. On failure, clean partial new exports, retain prior committed revisions, and record diagnostics. Exercise failures during each write and rename.

The spatial export metadata includes model and prompt revision, report and package hashes, processing parameters, coordinate systems, map registration provenance, reviewer actions, and approval time. Keep full-precision approved geometry for analysis. Preview simplification must never replace it.

## Embedded maps and scanned material

After the prepared-layer workflow is reliable, inspect the pilot reports for image-only pages, map figures, vector diagrams, and legends. Extract map images or render relevant PDF pages with document and page identifiers. Extract DOCX image relationships where needed, retaining available captions. A PDF renderer such as PyMuPDF is an optional dependency for this milestone, subject to format testing and license review.

Georeference usable maps with control points against known reference data. Store the image, CRS, control points, transformation method, residuals, and independent registration checks. QGIS already supports this workflow. A low control-point residual alone does not establish accuracy elsewhere on the map. Schematic diagrams may support relative relationships while remaining unsuitable for geographic polygon export. [QGIS georeferencing documentation](https://docs.qgis.org/3.44/en/docs/user_manual/managing_data_source/georeferencer.html).

Start with human-registered maps and digitized regions. If vision is required, extend the client with a separate image request path and model configuration only after verifying provider support. Send bounded image crops with their map references; validate pixel-region output, transform it using the registered map, and review the resulting geometry. Distinguish event boundaries from contours, symbols, legend swatches, and map frames.

Add OCR only when the input corpus requires it. Record OCR-derived text and its page coordinates and confidence; keep the original page available for review. Adapt evidence validation for OCR provenance without silently treating reconstructed text as original extracted text. Preserve PDF collection page mappings when splitting reports. Neither vision nor OCR should weaken original-text evidence checks for existing reports.

## Dependencies and installation

Use an optional `requirements-geospatial.txt` for Rasterio, Shapely, and pyproj, declaring NumPy directly if application code imports it. Rasterio covers raster reading, masks, reprojection, and polygonization; Shapely covers polygon operations and validation; pyproj covers explicit vector coordinate transformations. GeoJSON and package manifests use the standard JSON library. No spatial database, agent framework, vector database, or fine-tuning pipeline is required for the initial release.

Test installation in the project's supported Windows Python environment before selecting and recording compatible versions. Rasterio depends on GDAL and publishes binary wheels, but its documentation cautions that wheels are not tested for compatibility with every other binary package or QGIS. Keep the app environment separate from QGIS's bundled Python, and test the exact required formats. [Rasterio installation documentation](https://rasterio.readthedocs.io/en/stable/installation.html).

Shapely operations are planar and assume a common coordinate plane; reprojection must happen outside Shapely. pyproj supports explicit transformation axis handling. Verify both with a small known polygon and a known coordinate. [Shapely manual](https://shapely.readthedocs.io/en/stable/manual.html), [pyproj Transformer documentation](https://pyproj4.github.io/pyproj/stable/api/transformer.html).

Vendor the Leaflet assets with their license and version, using its GeoJSON and image-overlay capabilities. Basemap services, vision providers, and OCR introduce separate network or installation requirements only when enabled. [Leaflet reference](https://leafletjs.com/reference.html).

## Files to change

The following are implementation targets, not files created by this planning task. Keep the new backend in two small modules initially: GIS preparation and localization. Split them only when implementation size justifies it.

| File | Planned change |
| --- | --- |
| `multi_hazard_pipeline/geospatial.py` | New package registration, CRS checks, raster summaries, candidate geometry, validation, and spatial artifact helpers |
| `multi_hazard_pipeline/agents/localization_agent.py` | New assertion extraction and candidate selection through the existing JSON client |
| `multi_hazard_pipeline/agents/__init__.py` | Expose localization functionality consistently with existing agents |
| `multi_hazard_pipeline/schemas.py` | New spatial context, assertion, localization, and evaluation schemas and semantic validators |
| `multi_hazard_pipeline/config.py` | Optional spatial configuration, candidate and resource limits, processing parameters |
| `multi_hazard_pipeline/pipeline.py` | Spatial artifact registration, manifest substates, initial refresh, shared review readiness, collection association |
| `multi_hazard_pipeline/human_review.py` | Localization correction branch, spatial edits, revision checks, approval gates, coordinated export publication |
| `multi_hazard_pipeline/web.py` | Package selection, queued spatial work, map routes, correction actions, candidate and final download gates |
| `multi_hazard_pipeline/cli.py` | Package import, localization command, spatial run option, localization correction routing |
| `multi_hazard_pipeline/templates/index.html` | Optional package selection and enabled-run creation |
| `multi_hazard_pipeline/templates/run.html` | Map, spatial evidence, candidate selection, limitations, reviewer acknowledgements, spatial downloads |
| `multi_hazard_pipeline/static/map.js` and `style.css` | Map interactions and accessible spatial presentation; local Leaflet assets |
| `multi_hazard_pipeline/static/status.js` | Spatial processing and failure status where existing polling needs additional fields |
| `requirements-geospatial.txt` | Optional GIS dependency versions selected after Windows verification |
| `README.md` | Installation, package preparation, workflow, corrections, export meanings, and limitations |
| `tests/test_geospatial.py`, `tests/test_localization.py` | Small synthetic raster and polygon tests plus fake-model localization checks |
| Existing workflow and web tests | Spatial invalidation, approval, export rollback, collection associations, and legacy compatibility |

For the imagery milestone, also extend `agents/source_agent.py`, `splitter.py`, and `llm.py` where extraction, original-page mapping, and image requests require changes. Do not modify the classification taxonomy to carry spatial metadata.

## Delivery sequence and acceptance gates

### Phase 1 Validate inputs and install the GIS runtime

Resolve pilot data questions, inspect representative reports and maps, prepare one package, verify Windows dependencies, and implement package validation and schemas. Prepare a tiny synthetic DEM and polygons for tests.

Gate: one real package imports reproducibly; bad CRS, invalid paths, duplicate IDs, and unreadable rasters produce actionable errors; ordinary report processing still works without optional GIS packages.

### Phase 2 Localize segments against prepared areas

Implement terrain summaries, geographic assertions, name resolution, candidate construction, candidate selection, and spatial validation. Produce inspectable spatial artifacts from an existing corrected report. Use fake responses for automated tests and a deliberately bounded live evaluation for the pilot.

Gate: every segment has one localization record; multiple areas and unresolved outcomes work; unknown candidate IDs and unsupported quotes are rejected; all constructed geometry traces to package inputs and permitted operations.

### Phase 3 Integrate review and approved export

Integrate initial runs, text edits, localization corrections, package changes, collection children, map display, manual GeoJSON replacement, revision acknowledgements, approval, and downloads. Add local hillshade previews and export publication recovery.

Gate: a reviewer can complete a spatial run; changing a segment blocks stale spatial approval; candidates cannot be downloaded as authoritative results; a failed export leaves no partially approved result; existing text-only tests pass.

### Phase 4 Add map evidence where required

Add page and figure extraction, preserved original-page references, human registration metadata, and digitized event areas. Introduce vision and OCR only for the demonstrated input requirements, with dedicated capability and evidence tests. Automate terrain-unit preparation through established GIS tools if measured preparation effort warrants it.

Gate: one representative embedded map can be linked to a segment through reproducible registration and checked geometry; schematic or unregistrable maps are handled explicitly; image or OCR failures cannot silently replace text evidence.

### Phase 5 Evaluate the study and decide release scope

Run the predefined pilot comparison, review errors, document processing and review effort, and test the chosen configuration on held-out reports. Finalize required accuracy thresholds and operational instructions before broader adoption.

Gate: the study reports accuracy, uncertainty, coverage, and human effort together, and the supported input types and geographic precision are stated explicitly. If fine boundaries remain unreliable, release feature-level localization with those limitations rather than claiming footprint reconstruction.

## Automated checks and manual verification

Reuse pytest and the project's fake clients and network-blocking test setup. Tests need only small generated rasters and synthetic features; do not add confidential reports or large geographic datasets to Git.

Required checks cover:

- CRS reprojection and axis order against known coordinates; metre-based area and buffers; NoData and partial raster coverage; flat-terrain aspect and raster edge behavior.
- Polygon validity, holes, disconnected areas, study-boundary crossing, deterministic candidate IDs, bounded windows, and immutable package checksums.
- Ambiguous place names, multilingual aliases, contextual locations, elevation units, negation, relative direction versus aspect, multiple roles, alternatives, and unresolved results.
- Exactly one localization per segment, real source quotes, allowed candidate IDs, payload limits, deterministic validation retries, and no model-generated executable operations.
- Initial execution, each text correction route, manual candidate edits, segment deletion or merging, package changes, localization-only correction, exhausted correction rounds, and stale reviewer submissions.
- Approval with valid geometry, explicit acceptance of unresolved results, missing enabled-run artifacts, wrong revisions, queued work, and export failure during staging or publication.
- Allowlisted assets and downloads, traversal attempts, malicious popup labels, background failures, map interaction and keyboard access, and disabled GIS behavior.
- PDF collection package selection and original-page mapping; image and OCR provenance when Phase 4 is implemented.

Run targeted new tests during each phase, then `python -m pytest -q` when integration is complete. Verify the browser map manually using a known location and open approved GeoJSON in QGIS to confirm position, geometry, area interpretation, IDs, and attribution. Tests for the plan itself are unnecessary; these are requirements for the future implementation.

## Study evaluation

Annotate report segments before examining system predictions. Experts should record the correct geographic feature, the boundary meaning, acceptable extent or alternative regions, evidence, and whether localization is possible. Measure reviewer agreement and adjudicate disagreements; the reference must not demand more precision than the report supports.

Compare text with a gazetteer, text with reference maps and area layers, and the same inputs with DEM-derived information. Add registered report-map evidence as a separate configuration when available. Keep the segment definitions, report splits, and review protocol consistent. Partition by report, and by catchment where possible, so near-identical geography does not leak between development and evaluation.

Measure feature selection accuracy, resolved coverage, appropriate abstention, unsupported footprint claims, expert correction frequency, review time, processing latency, and request volume. For supported reference footprints, measure polygon intersection over union and omission and commission area in the analysis CRS. For broad or ambiguous descriptions, use acceptable-region agreement and evidential precision instead of scoring against an invented exact outline.

Report raw model results separately from human-corrected results. Compare review time with an expert drawing or selecting areas manually. Include difficult and unresolved cases. Archive report hashes, package revisions, prompt and model configuration, annotations, and evaluated geometry so the comparison can be reproduced.

Research such as GeoLM supports studying geographic entity recognition, linking, and relation extraction. It does not supply a demonstrated accuracy for this project's event-area task; that must come from this evaluation. [GeoLM paper](https://arxiv.org/abs/2310.14478).

## Scope and remaining risks

The initial release can provide useful area localization without a database, external geocoder, model fine-tuning, or autonomous map digitization. The principal scientific risks are insufficient report detail, ambiguous landmarks, changed terrain, map registration errors, coarse or unsuitable elevation data, and treating plausible terrain as observed evidence.

The principal implementation risks are Windows GIS installation, excessive raster memory, stale geography after corrections, unsupported image capabilities, and inconsistent publication of report and spatial revisions. The phases and acceptance gates above address these before broad use.

The plan is complete when a bounded report can move from supplied geographic inputs through evidence-backed area selection to reviewed exports, while uncertainty and failed processing remain distinguishable. Broader autonomy should be added only in response to measured limitations of that working system.
