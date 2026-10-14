# segment_mapping — fixed toolkit for placing report segments on a map

The toolkit places the segments extracted from an event report on a map. Every generic step is a command, so the same code runs every time:
- reading the report;
- downloading data;
- terrain models;
- georeferencing report figures;
- building and validating the shapefile;
- the map, README, report and ZIP.

The model (or analyst) only makes decisions, written as data in `[run folder]/scripts/placements.json` (see [SPEC.md](SPEC.md)).

```
python -m segment_mapping <command> RUN_FOLDER [options]      # --help on any command
```

Use the project environment: `.venv\Scripts\python.exe -m segment_mapping ...` from the project folder.

## Run a mapping task with a model

1. Setup, once per machine: Python 3.11+ and `python -m venv .venv`, then `.venv\Scripts\python.exe -m pip install -r requirements.txt` from the project folder. Internet access is needed for downloads. Edge or Chrome is optional; it is used to check that the map loads.
2. Prepare the inputs:
   - the report PDF (keep it outside the run folder);
   - the segment table as a TSV/CSV with a header row (`Segment, Event, Process, Generalized Category, Interaction Type, Sediment Transport Phase`);
   - optionally, a DEM and a catchment outline.
3. Generate the prompt. Use a new run folder for every model/run:

   ```
   .venv\Scripts\python.exe -m segment_mapping prompt --report REPORT.pdf --segments SEGMENTS.tsv --run output\EVENT_MODEL ^
       [--dem DEM.tif --dem-info "source, resolution, CRS, licence"] [--catchment CATCHMENT.gpkg --catchment-info "name, code, area"] --out prompt.txt
   ```

   The template is [prompt_template.txt](prompt_template.txt). It contains the mapping rules, the deliverables and the mandatory toolkit workflow.
4. Give `prompt.txt` to the model. Its working directory must be the project folder, and it needs shell and file tools and the ability to view images.
5. For a blind/comparative run, first move worked answers for that report out of the project folder: earlier run folders in `output/`, answer keys.

## Workflow

| Step | Command | Output |
| --- | --- | --- |
| 1. Create the run | `init RUN --report REPORT.pdf --segments SEGMENTS.tsv [--dem DEM.tif] [--catchment CATCHMENT.gpkg] [--preset tirol \| --crs EPSG:xxxx] [--event NAME]` | `run.json`, folders. The report is **not** copied. A provided DEM/catchment is imported, and the bbox is set from the catchment + 1 km. |
| 2. Read the report | `report RUN` | `data_raw/report_text.json`, `report_captions.json`, `report_images.json` (native figures), `report_tables.json`; page renders and contact sheets in `tmp/report_inspection/`. The printed-page offset is detected automatically. |
| | `text RUN [--page N] [--grep REGEX]`, `quote RUN --start .. --end ..` | Text search; quotation check with pages. |
| | `render RUN --image FILE \| --page N [--crop x0,y0,x1,y1] [--zoom 2] [--grid 50] [--mark "x,y;x,y"] [--polygon "x,y;..."]` | Zoomed crops with a pixel grid, for reading details and GCP/outline pixel coordinates and checking them. |
| 3. Locate the area | `locate TERM... --run RUN [--preset tirol]` | Official gazetteer matches (spelling variants such as ei/ai, umlauts) and OSM Nominatim, with extents in the run CRS. |
| | `config RUN --bbox x0,y0,x1,y1 \| --bbox-wgs84 ... \| --bbox-from LAYER --margin 1000 [--detail-bbox ...]` | Working area. The detail bbox is used for OSM, the fine DEM and orthophotos. |
| 4. Data | `inventory RUN` | Rule-2 list of what is present and missing (`data_raw/inventory.md`). |
| | `import RUN FILE --name NAME --role ROLE` | Any provided local vector/raster (one copy in the run CRS; rasters in the same CRS are not resampled). |
| | `import-package RUN package.json` · `export-package RUN OUT_DIR` | Import or write a spatial package (see below). |
| | `import-text RUN --pipeline-run DIR` | Use the report text (original + English) of a pipeline run instead of parsing the PDF (no figures). |
| | `facts RUN` | Candidate features with short ids (S#, C#, B#, R#, P#, K#), name mentions, stream/catchment facts (`data_raw/facts.json`). |
| | `fetch RUN preset tirol [--only vector,dem,ortho,osm] [--skip ...]` | Region preset (`presets` lists them). |
| | `fetch RUN osm` · `fetch RUN dem-global` | Anywhere: OpenStreetMap (Overpass with fallback to the OSM API); Copernicus GLO-30 DEM. |
| | `fetch RUN arcgis --url ... [--layer-name ...] --name N [--where SQL]` · `wfs` · `wcs` · `image --type arcgis_export\|wms` · `file --url URL_OR_PATH` | Any official service found online. Add `--provider --licence --licence-url --use --role`. Prefer `fetch arcgis` for ArcGIS layers: it pages through all features. `fetch file` with a REST `query` URL gets at most the server's record limit (a warning is printed when truncated). |
| | `source RUN --name ... --kind reference\|failed --url ... --note ...` | Records a consulted or inaccessible source (it appears in the report). |
| 5. Identify features | `clues RUN` | `outputs/geographic_clues.csv`: passages with place, quantity and time clues, plus gazetteer names found in the report. |
| | `find RUN REGEX [--layer L] [--near x,y --radius r]`, `fields RUN LAYER` | Feature ids (`fid_`, `osm_id`) and attribute values. |
| | `plot RUN --bbox ... --layers a,b --labels a:fid_ [--raster ortho_current\|hillshade] [--refs "x\|{json}"] [--parts] [--out NAME]` | PNG in `tmp/plots/` for visual matching and QA (`find` searches all attribute values, numeric ones included). |
| | `probe RUN EXPR [--param v] [--measure JSON] [--chainage NAME] [--save NAME]` | Evaluates an expression or measure without building. |
| 6. Decide | write `scripts/placements.json` | Refs, chainage, GCPs, ledgers, parts, checks, gaps, notes. |
| | `georef RUN [--id ID]` | Residuals, independent checks, warped QA overlay. |
| | `build RUN [--only 3a,6] [--dry]` | `outputs/segments.shp` (+ .shx .dbf .prj .cpg), `segments_evidence.csv`, `checks.json`. Prints errors and warnings; iterate to 0 errors. |
| 7. Deliver | `deliver RUN` | `segments_map.html`, `README.md`, `data_report.md`, `<event>_review.zip`, final validation, and a headless Edge/Chrome load check (`map_check.json`). |
| | `validate RUN`, `serve RUN` | Re-validate; serve `outputs/` for a manual browser check. |

Every command is appended to `scripts/commands.log`. Together with `placements.json`, this makes the run reproducible and lists it in the report.

## Text-only localization from the pipeline (constrained orchestration)

For a model without vision or tools (e.g. the pipeline's own LLM), the pipeline drives the toolkit itself:

```
python -m multi_hazard_pipeline localize RESULTS_RUN_DIR --package package.json [--segments-table T.tsv] [--only 3,4] [--no-review]
```

- It runs only on **approved** runs and writes everything to `RESULTS_RUN_DIR/localization/`, a normal toolkit run folder. A `localization` block is added to the run manifest.
- The model makes small, schema-checked choices:
  - one frame call: main stream, catchment, named features, hm anchors;
  - one call per segment: methods from a fixed menu with candidate ids and parameters;
  - repair calls when needed, and an optional self-review.
- Code builds every geometry and decides the placement class. It applies rule 5d and verifies quotations.
- Design: [LOCALIZATION_STAGE_DESIGN.md](LOCALIZATION_STAGE_DESIGN.md).

## Spatial package

The package is a folder with `package.json` plus the data files. It declares each layer's role and fields, so name matching works in any region:

```json
{"name": "...", "crs": "EPSG:31254",
 "layers": [{"name": "streams", "path": "streams.gpkg", "role": "streams", "name_field": "GEW_NAME_HAUPT", "id_field": "HZB_GEW"}, ...],
 "rasters": [{"name": "dem", "path": "dem.tif", "role": "dem"}],
 "sources": [{"name": "streams", "provider": "...", "licence": "...", "date": "..."}]}
```

- **Required roles:** `catchment`, `streams`, `dem`.
- **Useful roles:** `roads` (`ref_field` for road codes such as L13), `road_km` (`value_field`), `bridges`, `buildings`, `names`, `road_land`.
- **Building a package:** `export-package` writes one from any run, e.g. one filled once with `fetch preset tirol`.

## Map

`segments_map.html` is self-contained: Leaflet 1.9.4 (BSD-2, `vendor/leaflet/`) and all data are embedded, and a hillshade from the run DEM is included. It works offline. Web base maps from the preset are optional layers. Set `"offline_map": true` in `run.json` to open on the hillshade; text-only runs do this by default.

## Run folder

```
RUN/run.json                 settings (report path, CRS, page offset, bbox, segments, rasters, basemaps)
RUN/data_raw/                one projected copy per dataset: sources.gpkg (all vector layers), *.tif, source_manifest.json,
                             report_text.json, captions, figure list, georeferencing.json, visual_evidence.json, inventory.md
RUN/scripts/                 placements.json, commands.log, segments_input.tsv
RUN/outputs/                 deliverables (+ checks.json, geographic_clues.csv, map_check.json)
RUN/tmp/                     page renders, figure crops, QA plots (never zipped)
```

## Presets

`presets/*.json` describe regional data sources as data:
- `global`: OSM + Copernicus DEM, works everywhere.
- `tirol`: Land Tirol OGD catchments, streams, roads, bridges, road km, road parcels, names, building points, hazard zones, ALS DEM 5 m / 1 m, current and 2013–2015 orthophotos, flight-date metadata, plus OSM.

Layers are resolved by name, so changed service ids are handled and noted.

To add a region, copy `tirol.json` and list its services. Item types: `arcgis`, `wcs`, `arcgis_export`, `wms`, `osm`, `copernicus_dem`, `file`.

## Tests

`python -m unittest test_segment_mapping` runs offline on synthetic data.

A full regression with real data is kept **outside** the project folder: `Documents/segment_mapping_regression/seigesbach_placements.json`. It reproduces the Seigesbach_Sol_pictures run (22 of 24 parts geometrically identical). It is a worked answer key, so never place it where a model evaluated on that report can read it. To use it, copy it to `RUN/scripts/placements.json` of a run fetched with the Tirol preset, then run `build` and `deliver`.
