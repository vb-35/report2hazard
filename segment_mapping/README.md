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

`python -m pytest test_segment_mapping.py` runs offline on synthetic data.

A full regression with real data is kept **outside** the project folder: `Documents/segment_mapping_regression/seigesbach_placements.json`. It reproduces the Seigesbach_Sol_pictures run (22 of 24 parts geometrically identical). It is a worked answer key, so never place it where a model evaluated on that report can read it. To use it, copy it to `RUN/scripts/placements.json` of a run fetched with the Tirol preset, then run `build` and `deliver`.
