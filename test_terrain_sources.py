"""Mapzen display terrain: tile mosaicking, projection and failed downloads."""
import tempfile
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

import segment_mapping  # configure the GIS runtime before importing rasterio
import numpy as np
import rasterio
from rasterio.io import MemoryFile
from rasterio.transform import from_bounds

from multi_hazard_pipeline.errors import PipelineError
from multi_hazard_pipeline.terrain_sources import fetch_online_dem


class MapzenTerrainTests(TestCase):
    def test_four_tiles_mosaic_in_projected_metres(self):
        original_open = rasterio.open
        requested = []
        memories = []

        def open_tile(path, *args, **kwargs):
            if not str(path).startswith("https://"):
                return original_open(path, *args, **kwargs)
            requested.append(path)
            self.assertIn("elevation-tiles-prod.s3.amazonaws.com/geotiff/", path)
            z, x, y = map(int, path.removesuffix(".tif").split("/")[-3:])
            span = 40075016.68557849 / 2 ** z
            west = -20037508.342789244 + x * span
            north = 20037508.342789244 - y * span
            mem = MemoryFile()
            memories.append(mem)
            with mem.open(driver="GTiff", width=512, height=512, count=1,
                          dtype="float32", crs="EPSG:3857",
                          transform=from_bounds(west, north-span, west+span, north, 512, 512)) as ds:
                ds.write(np.full((512, 512), 100 + (x % 2) * 100 + (y % 2) * 200, dtype="float32"), 1)
            return mem.open()

        try:
            with tempfile.TemporaryDirectory() as folder:
                output = Path(folder) / "mapzen.tif"
                with patch("rasterio.open", side_effect=open_tile):
                    fetch_online_dem(output, (-1000, -1000, 1000, 1000), "EPSG:3857")
                with original_open(output) as ds:
                    values = ds.read(1)
                    self.assertEqual(ds.shape, (399, 399))
                    self.assertEqual(ds.crs.to_epsg(), 3857)
                    self.assertTrue(np.isfinite(values).all())
                    self.assertEqual(len({values[30, 30], values[30, -30], values[-30, 30], values[-30, -30]}), 4)
                self.assertEqual(len(requested), 4)
        finally:
            for mem in memories:
                mem.close()

    def test_failed_download_leaves_no_cached_dem(self):
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "mapzen.tif"
            with patch("rasterio.open", side_effect=OSError("unavailable")):
                with self.assertRaisesRegex(PipelineError, "Mapzen Terrain Tiles"):
                    fetch_online_dem(output, (-1000, -1000, 1000, 1000), "EPSG:3857")
            self.assertFalse(output.exists())


class CopernicusTerrainTests(TestCase):
    def test_public_cog_region_is_reprojected_and_cached_atomically(self):
        from multi_hazard_pipeline.terrain_sources import fetch_copernicus_dem
        original_open = rasterio.open
        requests = []
        with MemoryFile() as memory:
            with memory.open(driver="GTiff", width=32, height=32, count=1, dtype="float32",
                             crs="EPSG:4326", transform=from_bounds(10, 47, 11, 48, 32, 32)) as ds:
                ds.write(np.full((32, 32), 1234, dtype="float32"), 1)
            def open_tile(path, *args, **kwargs):
                if str(path).startswith("https://"):
                    requests.append(str(path))
                    return memory.open()
                return original_open(path, *args, **kwargs)
            with tempfile.TemporaryDirectory() as folder:
                output = Path(folder) / "copernicus.tif"
                with patch("rasterio.open", side_effect=open_tile):
                    fetch_copernicus_dem(output, (10.2, 47.2, 10.8, 47.8), "EPSG:4326")
                self.assertEqual(len(requests), 1)
                self.assertIn("Copernicus_DSM_COG_10_N47_00_E010_00_DEM", requests[0])
                with original_open(output) as ds:
                    self.assertTrue(np.allclose(ds.read(1), 1234))
                self.assertEqual([p.name for p in Path(folder).iterdir()], ["copernicus.tif"])
