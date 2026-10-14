"""Run folder layout, run.json configuration and local data access."""
from __future__ import annotations

import csv
import datetime as _dt
import io
import json
import sys
from functools import cached_property
from pathlib import Path

CONFIG = "run.json"
SPEC = "scripts/placements.json"
SOURCES_GPKG = "data_raw/sources.gpkg"
MANIFEST = "data_raw/source_manifest.json"
REPORT_TEXT = "data_raw/report_text.json"
INSPECT = "tmp/report_inspection"
PLOTS = "tmp/plots"

DEFAULT_CAPTION_PREFIXES = ["Abbildung", "Abb.", "Figure", "Fig.", "Figura", "Tabelle", "Tab.", "Table", "Tableau"]
OSM_BASEMAP = {"name": "OpenStreetMap", "url": "OpenStreetMap", "attr": "© OpenStreetMap contributors"}
ESRI_IMAGERY = {
    "name": "Esri World Imagery (reference only)",
    "url": "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
    "attr": "Esri, Maxar, Earthstar Geographics",
}


def today() -> str:
    return _dt.date.today().isoformat()


def utf8_stdout() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


def read_json(path: Path, default=None):
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def parse_segment_table(text: str) -> tuple[list[dict], list[str]]:
    """Parse a tab/comma/semicolon separated segment table with a header row.

    Values are kept exactly as given (including trailing spaces).
    """
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if not lines:
        raise ValueError("Segment table is empty")
    sample = "\n".join(lines[:5])
    delim = "\t" if "\t" in sample else csv.Sniffer().sniff(sample, delimiters=",;|").delimiter
    rows = list(csv.reader(io.StringIO("\n".join(lines)), delimiter=delim))
    header = [h.strip() for h in rows[0]]

    def col(*keys):
        for i, h in enumerate(header):
            low = h.lower()
            if any(k in low for k in keys):
                return i
        return None

    idx = {
        "seg_id": col("segment", "seg", "id"),
        "event": col("event"),
        "process": col("process"),
        "category": col("categ"),
        "interact": col("interact"),
        "phase": col("phase", "transport"),
    }
    if idx["seg_id"] is None or idx["process"] is None:
        raise ValueError(f"Segment table needs at least a segment ID and a process column; header was {header}")
    segments = []
    for r in rows[1:]:
        r = r + [""] * (len(header) - len(r))
        seg = {k: (r[i] if i is not None else "") for k, i in idx.items()}
        seg["seg_id"] = seg["seg_id"].strip()
        seg["extra"] = {header[i]: r[i] for i in range(len(header)) if i not in idx.values()}
        segments.append(seg)
    ids = [s["seg_id"] for s in segments]
    if len(set(ids)) != len(ids):
        raise ValueError(f"Duplicate segment IDs: {ids}")
    return segments, header


class Run:
    """A run folder: data_raw/ (downloads), scripts/ (spec + command log), outputs/, tmp/."""

    def __init__(self, folder: str | Path):
        self.dir = Path(folder).resolve()
        self._layers: dict = {}
        self._rasters: dict = {}

    # ------------------------------------------------------------------ paths
    def path(self, rel: str | Path) -> Path:
        p = Path(rel)
        return p if p.is_absolute() else self.dir / p

    @property
    def raw(self) -> Path:
        return self.dir / "data_raw"

    @property
    def out(self) -> Path:
        return self.dir / "outputs"

    @property
    def inspect(self) -> Path:
        return self.dir / INSPECT

    @property
    def plots(self) -> Path:
        return self.dir / PLOTS

    def ensure_dirs(self) -> None:
        for d in ("data_raw", "scripts", "outputs", INSPECT, PLOTS):
            (self.dir / d).mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ config
    @cached_property
    def config(self) -> dict:
        cfg = read_json(self.dir / CONFIG)
        if cfg is None:
            raise SystemExit(f"{self.dir / CONFIG} not found. Run `python -m segment_mapping init {self.dir} ...` first.")
        return cfg

    def save_config(self) -> None:
        write_json(self.dir / CONFIG, self.config)

    @property
    def crs(self) -> str:
        return self.config["crs"]

    @property
    def epsg(self) -> int:
        return int(str(self.crs).split(":")[-1])

    @property
    def report_path(self) -> Path:
        return Path(self.config["report"])

    @property
    def segments(self) -> list[dict]:
        return self.config["segments"]

    def segment(self, seg_id) -> dict:
        for s in self.segments:
            if s["seg_id"] == str(seg_id):
                return s
        raise KeyError(f"Segment {seg_id!r} is not in the segment table ({[s['seg_id'] for s in self.segments]})")

    @property
    def page_offset(self) -> int:
        return int(self.config.get("printed_page_offset") or 0)

    def printed(self, pdf_page: int) -> int:
        return pdf_page + self.page_offset

    def pdf_page(self, printed_page: int) -> int:
        return printed_page - self.page_offset

    def bbox(self, key: str = "bbox"):
        b = self.config.get(key)
        return tuple(b) if b else None

    # ------------------------------------------------------------------ logging
    def log_command(self, argv: list[str]) -> None:
        log = self.dir / "scripts" / "commands.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open("a", encoding="utf-8") as f:
            f.write(f"{_dt.datetime.now().isoformat(timespec='seconds')}\tpython -m segment_mapping {' '.join(argv)}\n")

    # ------------------------------------------------------------------ manifest
    def manifest(self) -> list[dict]:
        return read_json(self.dir / MANIFEST, [])

    def record_source(self, rec: dict) -> None:
        """Insert or replace a manifest record keyed by name."""
        rec.setdefault("accessed", today())
        items = [r for r in self.manifest() if r.get("name") != rec["name"]]
        items.append(rec)
        write_json(self.dir / MANIFEST, items)

    # ------------------------------------------------------------------ vector layers
    @property
    def gpkg(self) -> Path:
        return self.dir / SOURCES_GPKG

    def layer_names(self) -> list[str]:
        names = []
        if self.gpkg.exists():
            import pyogrio

            names += [n for n, _ in pyogrio.list_layers(self.gpkg)]
        digit = self.raw / "report_digitized.gpkg"
        if digit.exists():
            import pyogrio

            names += [f"digitized:{n}" for n, _ in pyogrio.list_layers(digit)]
        return names

    def layer(self, name: str):
        import geopandas as gpd

        if name not in self._layers:
            if name.startswith("digitized:"):
                g = gpd.read_file(self.raw / "report_digitized.gpkg", layer=name.split(":", 1)[1])
            elif ":" in name and not name.startswith("http"):
                path, lyr = name.rsplit(":", 1)
                g = gpd.read_file(self.path(path), layer=lyr)
            else:
                if name not in self.layer_names():
                    raise KeyError(f"Layer {name!r} not found. Available: {self.layer_names()}")
                g = gpd.read_file(self.gpkg, layer=name)
            if g.crs is None:
                g = g.set_crs(self.crs)
            elif g.crs.to_epsg() != self.epsg:
                g = g.to_crs(self.crs)
            self._layers[name] = g
        return self._layers[name]

    def write_layer(self, name: str, gdf, path: Path | None = None) -> int:
        """Write one projected copy of a dataset; adds a stable integer `fid_` column."""
        from pandas.api.types import is_object_dtype

        gdf = gdf.copy()
        if gdf.crs is None:
            raise ValueError(f"{name}: no CRS")
        if gdf.crs.to_epsg() != self.epsg:
            gdf = gdf.to_crs(self.crs)
        gdf = gdf[~gdf.geometry.isna() & ~gdf.geometry.is_empty].reset_index(drop=True)
        gdf["fid_"] = range(len(gdf))
        for c in gdf.columns:
            if c != "geometry" and is_object_dtype(gdf[c]):
                gdf[c] = gdf[c].map(lambda v: json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v)
        target = path or self.gpkg
        target.parent.mkdir(parents=True, exist_ok=True)
        gdf.to_file(target, layer=name, driver="GPKG", engine="pyogrio")
        self._layers.pop(name, None)
        return len(gdf)

    # ------------------------------------------------------------------ rasters
    def raster_path(self, name: str) -> Path:
        rasters = self.config.get("rasters", {})
        if name in rasters:
            return self.path(rasters[name])
        p = self.path(name)
        if p.exists():
            return p
        raise KeyError(f"Raster {name!r} not registered. Known: {sorted(rasters)}")

    def register_raster(self, name: str, rel: str) -> None:
        self.config.setdefault("rasters", {})[name] = rel
        self.save_config()

    def dem(self, name: str | None = None):
        from .terrain import Dem

        name = name or self.config.get("default_dem", "dem")
        if name not in self._rasters:
            self._rasters[name] = Dem(self.raster_path(name), name)
        return self._rasters[name]

    # ------------------------------------------------------------------ report text
    def pages(self) -> list[dict]:
        pages = read_json(self.dir / REPORT_TEXT)
        if pages is None:
            raise SystemExit("Report text not extracted yet. Run `python -m segment_mapping report RUN` first.")
        return pages

    # ------------------------------------------------------------------ spec
    def spec(self) -> dict:
        spec = read_json(self.dir / SPEC)
        if spec is None:
            raise SystemExit(f"{self.dir / SPEC} not found. Create it (see segment_mapping/SPEC.md).")
        return spec
