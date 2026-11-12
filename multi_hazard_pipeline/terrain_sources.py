"""Terrain used only for visualization; localization inputs remain unchanged."""
from __future__ import annotations

import hashlib
import math
import tempfile
from pathlib import Path

from .errors import PipelineError


MAPZEN_CREDIT = ("Mapzen Terrain Tiles · © offene Daten Österreichs – Digitales Geländemodell (DGM) Österreich; "
                 "Europe: produced using Copernicus data and information funded by the European Union (EU-DEM); "
                 "SRTM/GMTED2010: USGS. Sources and attribution: "
                 "https://github.com/tilezen/joerd/blob/master/docs/attribution.md")


def terrain_dem(base_dir, source, bounds, crs, upload=None):
    from .localization_compare import dem_file

    folder = base_dir / "localization" / "visualization"
    if source == "run":
        path = dem_file(base_dir)
        if path is None:
            raise PipelineError("No DEM is stored with this run. Choose an online elevation source or an elevation file.")
        return path, "Run elevation model"
    if source == "upload":
        path = folder / "uploaded.tif"
        if upload is not None:
            import rasterio
            from rasterio.warp import transform_bounds

            folder.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(dir=folder, suffix=".tif", delete=False) as tmp:
                pending = Path(tmp.name)
            try:
                upload.save(pending)
                with rasterio.open(pending) as ds:
                    if ds.driver != "GTiff" or not ds.crs or ds.count != 1:
                        raise PipelineError("Upload a single-band elevation GeoTIFF with a defined coordinate system and heights in metres.")
                    b = transform_bounds(ds.crs, crs, *ds.bounds)
                    if bounds and (b[2] <= bounds[0] or b[0] >= bounds[2] or b[3] <= bounds[1] or b[1] >= bounds[3]):
                        raise PipelineError("The DEM does not overlap this report's mapped area.")
                    sample = ds.read(1, out_shape=(100, 100), masked=True)
                    import numpy as np
                    if not np.ma.masked_invalid(sample).count():
                        raise PipelineError("The uploaded DEM contains no elevation data.")
                pending.replace(path)
            except PipelineError:
                raise
            except Exception as exc:
                raise PipelineError("Could not read the DEM. Upload a valid elevation GeoTIFF.") from exc
            finally:
                pending.unlink(missing_ok=True)
        if not path.is_file():
            raise PipelineError("Choose an elevation GeoTIFF to upload.")
        return path, "Uploaded elevation model (heights in metres)"
    if source not in ("online", "copernicus"):
        raise PipelineError("Choose a saved DEM, Mapzen, Copernicus, or an elevation file.")
    if bounds is None:
        raise PipelineError("Online terrain needs a catchment or mapped parts to determine the area.")
    digest = hashlib.sha256(repr((tuple(bounds), str(crs))).encode()).hexdigest()[:20]
    path = folder / f"{'mapzen' if source == 'online' else source}-{digest}.tif"
    if not path.is_file():
        folder.mkdir(parents=True, exist_ok=True)
        if source == "copernicus":
            fetch_copernicus_dem(path, bounds, crs)
        else:
            fetch_online_dem(path, bounds, crs)
    return path, MAPZEN_CREDIT if source == "online" else COPERNICUS_CREDIT


def fetch_online_dem(path, bounds, crs):
    """Mosaic Mapzen elevation GeoTIFF tiles onto a bounded display grid."""
    import numpy as np
    import rasterio
    from rasterio.enums import Resampling
    from rasterio.transform import from_bounds
    from rasterio.vrt import WarpedVRT
    from rasterio.warp import transform_bounds

    west, south, east, north = transform_bounds(crs, 4326, *bounds)
    if not all(math.isfinite(v) for v in (west, south, east, north)) or not (-180 <= west < east <= 180 and -90 <= south < north <= 90):
        raise PipelineError("The mapped area has invalid geographic bounds for online retrieval.")
    if south < -85.05112878 or north > 85.05112878:
        raise PipelineError("Mapzen Terrain Tiles cover latitudes up to 85.05°. Upload a DEM for this region.")
    width, height = bounds[2]-bounds[0], bounds[3]-bounds[1]
    step = max(width, height) / 399
    nx, ny = max(2, round(width/step)), max(2, round(height/step))
    # GeoTIFF tiles have 512 pixels. Choose a useful detail level and bound the
    # number of downloads, including areas spanning tile boundaries.
    latitude = (south + north) / 2
    zoom = max(0, min(14, math.ceil(math.log2(78271.51696 * math.cos(math.radians(latitude)) / step))))
    def tile_range(z):
        n = 2 ** z
        def x(lon):
            return max(0, min(n - 1, math.floor((lon + 180) / 360 * n)))
        def y(lat):
            return max(0, min(n - 1, math.floor((1 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2 * n)))
        return range(x(west), x(east) + 1), range(y(north), y(south) + 1)
    xs, ys = tile_range(zoom)
    while len(xs) * len(ys) > 16 and zoom > 0:
        zoom -= 1
        xs, ys = tile_range(zoom)
    transform = from_bounds(*bounds, nx, ny)
    grid = np.full((ny, nx), np.nan, dtype="float32")
    try:
        with rasterio.Env(GDAL_HTTP_CONNECTTIMEOUT="10", GDAL_HTTP_TIMEOUT="30", GDAL_HTTP_MAX_RETRY="1",
                          GDAL_DISABLE_READDIR_ON_OPEN="EMPTY_DIR", CPL_VSIL_CURL_ALLOWED_EXTENSIONS=".tif"):
            for x, y in ((x, y) for x in xs for y in ys):
                url = f"https://elevation-tiles-prod.s3.amazonaws.com/geotiff/{zoom}/{x}/{y}.tif"
                with rasterio.open(url) as src:
                    with WarpedVRT(src, crs=crs, transform=transform, width=nx, height=ny,
                                   resampling=Resampling.bilinear, dtype="float32", nodata=float("nan")) as vrt:
                        data = vrt.read(1, masked=True).filled(np.nan)
                        valid = np.isfinite(data)
                        grid[valid] = data[valid]
        if not np.isfinite(grid).all():
            raise ValueError("No elevation coverage")
        with tempfile.NamedTemporaryFile(dir=path.parent, suffix=".tif", delete=False) as tmp:
            pending = Path(tmp.name)
        try:
            with rasterio.open(pending, "w", driver="GTiff", width=nx, height=ny, count=1,
                               dtype="float32", crs=crs, transform=transform, nodata=np.nan, compress="deflate") as dst:
                dst.write(grid, 1)
            pending.replace(path)
        finally:
            pending.unlink(missing_ok=True)
    except Exception as exc:
        raise PipelineError("Online terrain could not be retrieved. Check your internet connection or upload a DEM; "
                            "Mapzen Terrain Tiles may have no coverage for this area.") from exc


COPERNICUS_CREDIT = ("Copernicus DEM GLO-30 (approximately 30 m surface model, includes buildings and vegetation). "
                     "© DLR e.V. 2010–2014 and © Airbus 2014–2018; provided under COPERNICUS by the EU and ESA.")


def fetch_copernicus_dem(path, bounds, crs):
    """Retrieve the requested region from public Copernicus GLO-30 COGs."""
    import numpy as np
    import rasterio
    from rasterio.enums import Resampling
    from rasterio.transform import from_bounds
    from rasterio.vrt import WarpedVRT
    from rasterio.warp import transform_bounds

    west, south, east, north = transform_bounds(crs, 4326, *bounds)
    if not all(math.isfinite(v) for v in (west, south, east, north)) or not (-180 <= west < east <= 180 and -90 <= south < north <= 90):
        raise PipelineError("Invalid geographic bounds for Copernicus terrain.")
    tiles = [(lat, lon) for lat in range(math.floor(south), math.floor(north)+1)
             for lon in range(math.floor(west), math.floor(east)+1)]
    if len(tiles) > 16:
        raise PipelineError("This region is too large. Use Mapzen full terrain or choose an elevation file.")
    step = max(bounds[2]-bounds[0], bounds[3]-bounds[1]) / 399
    nx, ny = max(2, round((bounds[2]-bounds[0])/step)), max(2, round((bounds[3]-bounds[1])/step))
    transform = from_bounds(*bounds, nx, ny)
    grid = np.full((ny, nx), np.nan, dtype="float32")
    try:
        with rasterio.Env(GDAL_HTTP_CONNECTTIMEOUT="10", GDAL_HTTP_TIMEOUT="30", GDAL_HTTP_MAX_RETRY="1",
                          GDAL_DISABLE_READDIR_ON_OPEN="EMPTY_DIR", CPL_VSIL_CURL_ALLOWED_EXTENSIONS=".tif"):
            for lat, lon in tiles:
                tile = f"Copernicus_DSM_COG_10_{'N' if lat >= 0 else 'S'}{abs(lat):02d}_00_{'E' if lon >= 0 else 'W'}{abs(lon):03d}_00_DEM"
                with rasterio.open(f"https://copernicus-dem-30m.s3.amazonaws.com/{tile}/{tile}.tif") as src:
                    with WarpedVRT(src, crs=crs, transform=transform, width=nx, height=ny,
                                   resampling=Resampling.bilinear, dtype="float32", nodata=float("nan")) as vrt:
                        data = vrt.read(1, masked=True).filled(np.nan)
                        valid = np.isfinite(data)
                        grid[valid] = data[valid]
        if not np.isfinite(grid).all():
            raise ValueError("Incomplete coverage")
        with tempfile.NamedTemporaryFile(dir=path.parent, suffix=".tif", delete=False) as tmp:
            pending = Path(tmp.name)
        try:
            with rasterio.open(pending, "w", driver="GTiff", width=nx, height=ny, count=1,
                               dtype="float32", crs=crs, transform=transform, nodata=np.nan, compress="deflate") as dst:
                dst.write(grid, 1)
            pending.replace(path)
        finally:
            pending.unlink(missing_ok=True)
    except Exception as exc:
        raise PipelineError("Copernicus elevation is unavailable for this area or could not be downloaded. Try Mapzen or choose an elevation file.") from exc
