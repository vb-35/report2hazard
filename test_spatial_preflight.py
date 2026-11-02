"""Offline, spatially explicit package checks."""
import json
from pathlib import Path

import geopandas as gpd
import numpy as np
import pytest
import rasterio
from rasterio.transform import from_origin
from shapely.geometry import LineString, box

from segment_mapping.package import PackageError, load_package, preflight_package


def spatial_package(tmp_path, *, nodata=False, width=10, crs="EPSG:31254"):
    gpd.GeoDataFrame(geometry=[box(0, 0, 100, 100)], crs=crs).to_file(tmp_path / "cat.gpkg")
    gpd.GeoDataFrame(geometry=[LineString([(50, 90), (50, 10)])], crs=crs).to_file(tmp_path / "stream.gpkg")
    band = np.ones((10, width), dtype="float32")
    if nodata:
        band[:, :2] = -9999
    with rasterio.open(tmp_path / "dem.tif", "w", driver="GTiff", width=width, height=10, count=1,
                       dtype="float32", crs=crs, transform=from_origin(0, 100, 10, 10), nodata=-9999) as ds:
        ds.write(band, 1)
    manifest = dict(crs=crs, layers=[dict(name="cat", role="catchment", path="cat.gpkg"),
                                   dict(name="streams", role="streams", path="stream.gpkg")],
                    rasters=[dict(name="dem", role="dem", path="dem.tif")])
    path = tmp_path / "package.json"
    path.write_text(json.dumps(manifest))
    return path


def test_verified_package_and_readable_reports(tmp_path):
    path = spatial_package(tmp_path)
    report = preflight_package(path, report_dir=tmp_path / "reports")
    assert report["ok"]
    assert report["rasters"][0]["coverage_fraction"] == pytest.approx(1)
    assert report["streams"][0]["dangling_endpoints"] == 2
    assert "unverified" in report["streams"][0]["orientation"]
    assert "PASS" in (tmp_path / "reports/preflight.txt").read_text()
    assert load_package(path)["crs"] == "EPSG:31254"


def test_coverage_uses_catchment_and_reports_nodata_and_outside(tmp_path):
    report = preflight_package(spatial_package(tmp_path, nodata=True, width=8))
    assert not report["ok"]
    dem = report["rasters"][0]
    assert dem["coverage_fraction"] == pytest.approx(.6)
    assert dem["gap_area_m2"] == pytest.approx(4000)
    assert {g["kind"] for g in dem["gaps"]} == {"nodata", "outside_raster"}


def test_missing_crs_is_rejected_without_assigning_manifest_crs(tmp_path):
    path = spatial_package(tmp_path)
    gpd.GeoDataFrame(geometry=[LineString([(50, 90), (50, 10)])]).to_file(tmp_path / "unknown.gpkg")
    manifest = json.loads(path.read_text())
    manifest["layers"][1]["path"] = "unknown.gpkg"
    path.write_text(json.dumps(manifest))
    with pytest.raises(PackageError, match="no CRS"):
        load_package(path)


@pytest.mark.parametrize("crs", ["EPSG:4326", "EPSG:2263"])
def test_target_must_be_projected_metres(tmp_path, crs):
    report = preflight_package(spatial_package(tmp_path, crs=crs))
    assert not report["ok"]
    assert "metre" in report["errors"][0]


def test_missing_core_inputs_remain_explicit(tmp_path):
    path = spatial_package(tmp_path)
    manifest = json.loads(path.read_text())
    manifest["rasters"] = []
    path.write_text(json.dumps(manifest))
    report = preflight_package(path)
    assert not report["ok"]
    assert "dem" in report["errors"][0]


def test_invalid_polygon_is_rejected_without_repair(tmp_path):
    from shapely.geometry import Polygon
    path = spatial_package(tmp_path)
    gpd.GeoDataFrame(geometry=[Polygon([(0, 0), (100, 100), (100, 0), (0, 100), (0, 0)])], crs="EPSG:31254").to_file(tmp_path / "cat.gpkg")
    report = preflight_package(path)
    assert not report["ok"]
    assert any("invalid geometries" in error for error in report["errors"])


def test_valid_source_that_becomes_invalid_after_reprojection_is_rejected(tmp_path, monkeypatch):
    from shapely.geometry import Polygon

    path = spatial_package(tmp_path)
    original = gpd.GeoDataFrame.to_crs

    def reproject(frame, *args, **kwargs):
        projected = original(frame, *args, **kwargs)
        if frame.geom_type.eq("Polygon").all():
            projected.geometry = [Polygon([(0, 0), (100, 100), (100, 0), (0, 100), (0, 0)])]
        return projected

    monkeypatch.setattr(gpd.GeoDataFrame, "to_crs", reproject)
    report = preflight_package(path)
    assert not report["ok"]
    assert any("invalid geometries after reprojection" in error and "Self-intersection" in error
               for error in report["errors"])


def test_missing_raster_crs_fails_preflight(tmp_path):
    path = spatial_package(tmp_path)
    with rasterio.open(tmp_path / "dem.tif", "w", driver="GTiff", width=10, height=10, count=1,
                       dtype="float32", transform=from_origin(0, 100, 10, 10)) as ds:
        ds.write(np.ones((10, 10), dtype="float32"), 1)
    report = preflight_package(path)
    assert not report["ok"]
    assert any("raster has no CRS" in error for error in report["errors"])


def test_supplemental_fan_dem_need_not_cover_entire_catchment(tmp_path):
    path = spatial_package(tmp_path)
    with rasterio.open(tmp_path / "fan.tif", "w", driver="GTiff", width=2, height=10, count=1,
                       dtype="float32", crs="EPSG:31254", transform=from_origin(0, 100, 10, 10)) as ds:
        ds.write(np.ones((10, 2), dtype="float32"), 1)
    package = json.loads(path.read_text())
    package["rasters"].append(dict(name="fan", path="fan.tif", role="dem"))
    path.write_text(json.dumps(package))
    report = preflight_package(path)
    assert report["ok"], report["errors"]
    assert report["primary_dem"] == "dem"
    assert any("supplemental DEM" in warning for warning in report["warnings"])
    package["preparation"] = {"default_dem": "fan"}
    path.write_text(json.dumps(package))
    report = preflight_package(path)
    assert not report["ok"]
    assert any("required 99.00%" in error for error in report["errors"])
