"""Fresh-process coverage for Windows environments polluted by PostGIS/QGIS."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from unittest import TestCase


class GisRuntimeTests(TestCase):
    def test_entrypoints_read_and_warp_epsg_geotiff_with_foreign_gis_paths(self):
        # Match the real DEM's CRS/coordinates without depending on local results.
        import segment_mapping  # configure this process before writing the fixture
        import numpy as np
        import rasterio
        from rasterio.transform import from_origin

        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "austrian-dem.tif"
            with rasterio.open(path, "w", driver="GTiff", width=20, height=20, count=1,
                               dtype="float32", crs="EPSG:31254", transform=from_origin(63500, 231900, 10, 10)) as dst:
                dst.write(np.arange(400, dtype="float32").reshape(20, 20) + 900, 1)
            env = os.environ.copy()
            # Every relevant inherited variable must be ignored by bundled wheels.
            for name in ("PROJ_LIB", "PROJ_DATA", "GDAL_DATA"):
                env[name] = str(Path(folder) / "foreign-postgis-data")
            for entrypoint in ("launch_interface", "multi_hazard_pipeline.web", "segment_mapping"):
                with self.subTest(entrypoint=entrypoint):
                    script = f"""
import {entrypoint}
from concurrent.futures import ThreadPoolExecutor
import numpy as np
import rasterio
from rasterio.vrt import WarpedVRT
from rasterio.enums import Resampling
from rasterio.warp import transform_bounds

def read_and_warp():
    with rasterio.open({str(path)!r}) as src:
        assert src.crs.to_epsg() == 31254, src.crs
        bounds = transform_bounds(src.crs, 'EPSG:4326', *src.bounds)
        assert all(np.isfinite(bounds)), bounds
        assert 9 < bounds[0] < 12 and 46 < bounds[1] < 49, bounds
        with WarpedVRT(src, crs='EPSG:32632', resampling=Resampling.bilinear) as vrt:
            z = vrt.read(1, masked=True)
            assert z.count() > 0 and z.max() > z.min(), z

read_and_warp()
with ThreadPoolExecutor(max_workers=1) as pool:
    pool.submit(read_and_warp).result()
"""
                    result = subprocess.run([sys.executable, "-c", script], env=env,
                                            capture_output=True, text=True, timeout=45)
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
