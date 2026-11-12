"""Keep bundled GIS libraries from using another application's resource databases."""
from importlib.util import find_spec
import os
from pathlib import Path


def configure_gis_runtime() -> None:
    """Run before importing native GIS libraries, including in CLI and web workers.

    Rasterio/Pyogrio wheels ship matching PROJ and GDAL data. An inherited
    PostGIS/QGIS installation can override those with an incompatible proj.db,
    causing even an EPSG-tagged GeoTIFF to be read as LOCAL_CS. Unset overrides
    in this process only, letting each library discover its own bundled data.
    System/Conda installations without bundled data retain their configuration.
    """
    for package in ("rasterio", "pyogrio"):
        spec = find_spec(package)
        if spec is None or spec.origin is None:
            continue
        folder = Path(spec.origin).parent
        if (folder / "proj_data" / "proj.db").is_file():
            os.environ.pop("PROJ_LIB", None)
            os.environ.pop("PROJ_DATA", None)
        if (folder / "gdal_data" / "gdalvrt.xsd").is_file():
            os.environ.pop("GDAL_DATA", None)
