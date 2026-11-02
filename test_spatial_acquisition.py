"""Acquisition contract tests use local data and mocked public-service responses."""
import json
import io
from unittest.mock import Mock, patch

import geopandas as gpd
import pytest
from shapely.geometry import Point

from segment_mapping.acquisition import REQUEST_SCHEMA, acquire_source, validate_request
from segment_mapping.runctx import Run, write_json


def streamed_response(data, *, status=200, after_read=None):
    """Real requests streaming semantics, without any network access."""
    import requests

    class Body(io.BytesIO):
        def read(self, size=-1):
            chunk = super().read(size)
            if after_read:
                after_read()
            return chunk

    response = requests.Response()
    response.status_code = status
    response.raw = Body(data)
    response.url = "https://official.example/data"
    return response


def raster_bytes(*, crs="EPSG:31254", width=10, height=10, x=0):
    import numpy as np
    from rasterio.io import MemoryFile
    from rasterio.transform import from_origin

    with MemoryFile() as memory:
        with memory.open(driver="GTiff", width=width, height=height, count=1,
                         dtype="float32", crs=crs, transform=from_origin(x, 100, 10, 10)) as ds:
            ds.write(np.ones((height, width), dtype="float32"), 1)
        return memory.read()


def wcs_package():
    return {"acquisition": {"providers": [dict(
        id="terrain", name="dem", role="dem", type="wcs", url="https://official.example/wcs",
        coverage="dem", res=10, version="2.0.1")]}}


def arcgis_package():
    return {"acquisition": {"providers": [dict(
        id="roads", name="roads", role="roads", type="arcgis",
        service="https://official.example/MapServer/0")]}}


def make_run(tmp_path):
    run = Run(tmp_path / "run")
    run.ensure_dirs()
    write_json(run.dir / "run.json", dict(crs="EPSG:31254", bbox=[0, 0, 100, 100]))
    return run


def test_strict_model_request_rejects_arbitrary_urls_and_sql():
    assert REQUEST_SCHEMA["additionalProperties"] is False
    for request in [{"role": "roads", "query": "place", "url": "https://bad"},
                    {"role": "roads", "query": "place", "where": "1=1"},
                    {"role": "roads", "query": ""}]:
        with pytest.raises(ValueError):
            validate_request(request)


def test_undeclared_and_network_disabled_are_explicit_and_logged(tmp_path):
    run = make_run(tmp_path)
    request = dict(role="roads", query="village")
    result = acquire_source(run, request)
    assert result["status"] == "failed"
    assert "no declared provider" in result["error"]
    package = {"acquisition": {"providers": [dict(id="roads", name="roads", role="roads", type="arcgis", service="https://official.example/MapServer")]}}
    with patch("segment_mapping.sources.http") as http:
        result = acquire_source(run, request, package=package)
        http.assert_not_called()
    assert "disabled" in result["error"]
    assert len(json.loads(run.path("data_raw/acquisition_history.json").read_text())) == 2


def test_local_file_is_usable_offline_registered_and_cached(tmp_path):
    run = make_run(tmp_path)
    source = tmp_path / "places.gpkg"
    gpd.GeoDataFrame({"NAME": ["Village"]}, geometry=[Point(50, 50)], crs="EPSG:31254").to_file(source)
    package = {"acquisition": {"providers": [dict(id="official", name="places", role="names", type="file", url=str(source), name_field="NAME")]}}
    request = dict(role="names", query="Village")
    result = acquire_source(run, request, package=package)
    assert result["status"] == "success", result
    assert result["source"]["count"] == 1
    assert result["downloaded_crs"] == "EPSG:31254"
    assert Run(run.dir).config["package"]["layers"][result["output_name"]]["role"] == "names"
    assert len(Run(run.dir).layer(result["output_name"])) == 1
    cached = acquire_source(run, request, package=package)
    assert cached["cache_hit"]
    assert not cached["attempts"]


def test_zero_query_result_is_gap_even_if_old_layer_exists(tmp_path):
    run = make_run(tmp_path)
    gpd.GeoDataFrame(geometry=[Point(50, 50)], crs=run.crs).to_file(run.gpkg, layer="roads")
    package = {"acquisition": {"providers": [dict(id="roads", name="roads", role="roads", type="arcgis", service="https://official.example/MapServer")]}}
    with patch("segment_mapping.sources.fetch_arcgis", return_value=dict(kind="vector", count=0)):
        result = acquire_source(run, dict(role="roads", query="Village"), package=package, allow_network=True)
    assert result["status"] == "failed"
    assert "zero features" in result["error"]
    assert result["data_gaps"]


def test_http_retries_are_bounded_and_query_is_literal(tmp_path):
    run = make_run(tmp_path)
    package = {"acquisition": {"providers": [dict(id="roads", name="roads", role="roads", type="arcgis", query_field="NAME", service="https://official.example/MapServer")]}}
    with patch("segment_mapping.sources.http", side_effect=RuntimeError("service unavailable")) as http:
        result = acquire_source(run, dict(role="roads", query="O'Brien"), package=package, allow_network=True, retries=2)
    assert result["status"] == "failed"
    assert len(result["attempts"]) == 2
    assert http.call_count == 2
    assert "O''BRIEN" in result["query_application"]["where"]


def test_wcs_missing_crs_is_rejected_before_relabel(tmp_path):
    import numpy as np
    from rasterio.io import MemoryFile
    from rasterio.transform import from_origin
    run = make_run(tmp_path)
    with MemoryFile() as memory:
        with memory.open(driver="GTiff", width=10, height=10, count=1, dtype="float32", transform=from_origin(0, 100, 10, 10)) as ds:
            ds.write(np.ones((10, 10), dtype="float32"), 1)
        data = memory.read()
    response = streamed_response(data)
    package = {"acquisition": {"providers": [dict(id="terrain", name="dem", role="dem", type="wcs", url="https://official.example/wcs", coverage="dem", res=10, version="2.0.1")]}}
    with patch("segment_mapping.sources.http", return_value=response):
        result = acquire_source(run, dict(role="dem", query="Village"), package=package, allow_network=True)
    assert result["status"] == "failed"
    assert "refusing relabel" in result["error"]
    assert not run.path("data_raw/dem.tif").exists()


def test_cache_invalidates_if_output_changes(tmp_path):
    run = make_run(tmp_path)
    source = tmp_path / "places.gpkg"
    gpd.GeoDataFrame({"NAME": ["Village"]}, geometry=[Point(50, 50)], crs=run.crs).to_file(source)
    package = {"acquisition": {"providers": [dict(id="places", name="places", role="names", type="file", url=str(source))]}}
    request = dict(role="names", query="Village")
    original = acquire_source(run, request, package=package)
    assert original["status"] == "success"
    run.write_layer(original["output_name"], gpd.GeoDataFrame(geometry=[Point(10, 10)], crs=run.crs))
    result = acquire_source(run, request, package=package)
    assert result["status"] == "success"
    assert not result["cache_hit"]
    assert Run(run.dir).layer(result["output_name"]).iloc[0].geometry.equals(Point(50, 50))


@pytest.mark.parametrize("kind", ["wcs", "arcgis", "file"])
def test_all_http_paths_stream_and_reject_oversized_bodies_before_decode(tmp_path, kind):
    run = make_run(tmp_path)
    data = raster_bytes() if kind != "arcgis" else b'{"name":"roads"}'
    response = streamed_response(data)
    response.json = Mock(side_effect=AssertionError("JSON must not be decoded"))
    package = wcs_package() if kind == "wcs" else arcgis_package()
    role = "dem" if kind != "arcgis" else "roads"
    if kind == "file":
        package = {"acquisition": {"providers": [dict(
            id="terrain", name="dem", role=role, type="file", url="https://official.example/dem.tif")]}}
    with patch("segment_mapping.sources.http", return_value=response) as http, \
            patch("rasterio.io.MemoryFile", side_effect=AssertionError("TIFF must not be decoded")), \
            patch("segment_mapping.sources.fetch_file") as fetch_file:
        result = acquire_source(run, dict(role=role, query="Village"), package=package,
                                allow_network=True, max_download_bytes=1)
    assert result["status"] == "failed", result
    assert "byte limit" in result["error"]
    assert http.call_count == 1
    assert http.call_args.kwargs["stream"] is True
    assert response.raw.closed
    response.json.assert_not_called()
    fetch_file.assert_not_called()


def test_arcgis_budget_is_cumulative_across_metadata_and_query(tmp_path):
    metadata = b'{"name":"roads"}'
    query = b'{"objectIds":[]}'
    responses = [streamed_response(metadata), streamed_response(query)]
    responses[1].json = Mock(side_effect=AssertionError("oversized query decoded"))
    with patch("segment_mapping.sources.http", side_effect=responses) as http:
        result = acquire_source(make_run(tmp_path), dict(role="roads", query="Village"),
                                package=arcgis_package(), allow_network=True,
                                max_download_bytes=len(metadata) + len(query) - 1)
    assert "byte limit" in result["error"], result
    assert result["download_bytes"] == len(metadata) + len(query)
    assert http.call_count == 2
    responses[1].json.assert_not_called()
    assert all(response.raw.closed for response in responses)


def test_retry_partial_body_counts_toward_download_budget(tmp_path):
    import requests

    class BrokenBody(io.BytesIO):
        def stream(self, size, decode_content=True):
            yield b"123456"
            raise requests.ConnectionError("interrupted stream")

    first = streamed_response(b"")
    first.raw = BrokenBody()
    second = streamed_response(b'{"name":"roads"}')
    with patch("segment_mapping.sources.http", side_effect=[first, second]) as http:
        result = acquire_source(make_run(tmp_path), dict(role="roads", query="Village"),
                                package=arcgis_package(), allow_network=True, retries=2,
                                max_download_bytes=20)
    assert "byte limit" in result["error"], result
    assert len(result["attempts"]) == http.call_count == 2
    assert first.raw.closed and second.raw.closed


def test_streaming_checks_elapsed_budget_before_decoding(tmp_path):
    now = [0]
    response = streamed_response(b'{"name":"roads"}', after_read=lambda: now.__setitem__(0, 2))
    response.json = Mock(side_effect=AssertionError("expired body decoded"))
    with patch("segment_mapping.acquisition.time.monotonic", side_effect=lambda: now[0]), \
            patch("segment_mapping.sources.http", return_value=response):
        result = acquire_source(make_run(tmp_path), dict(role="roads", query="Village"),
                                package=arcgis_package(), allow_network=True, max_elapsed_seconds=1)
    assert "elapsed-time budget" in result["error"], result
    assert response.raw.closed
    response.json.assert_not_called()


def test_wcs_actual_dimensions_checked_before_legacy_merge(tmp_path):
    response = streamed_response(raster_bytes(width=11))
    with patch("segment_mapping.sources.http", return_value=response), patch("rasterio.merge.merge") as merge:
        result = acquire_source(make_run(tmp_path), dict(role="dem", query="Village"),
                                package=wcs_package(), allow_network=True, max_raster_pixels=100)
    assert "pixel limit" in result["error"], result
    merge.assert_not_called()


def test_wcs_mosaic_extent_checked_before_legacy_merge(tmp_path):
    # Force two tiles without allocating a large test raster. Each tile fits the
    # budget, but the server returns the second tile far outside the request.
    tiles = [(0, 0, 5, 10, (0, 0, 50, 100)), (1, 0, 5, 10, (50, 0, 100, 100))]
    responses = [streamed_response(raster_bytes(width=5)), streamed_response(raster_bytes(width=5, x=10000))]
    with patch("segment_mapping.sources._image_tiles", return_value=tiles), \
            patch("segment_mapping.sources.http", side_effect=responses), patch("rasterio.merge.merge") as merge:
        result = acquire_source(make_run(tmp_path), dict(role="dem", query="Village"),
                                package=wcs_package(), allow_network=True, max_raster_pixels=100)
    assert "mosaic exceeds pixel limit" in result["error"], result
    merge.assert_not_called()


def test_local_and_cached_rasters_check_dimensions_before_read(tmp_path):
    import rasterio

    run = make_run(tmp_path)
    source = tmp_path / "dem.tif"
    source.write_bytes(raster_bytes())
    package = {"acquisition": {"providers": [dict(
        id="terrain", name="dem", role="dem", type="file", url=str(source))]}}
    request = dict(role="dem", query="Village")
    original = acquire_source(run, request, package=package, max_raster_pixels=100)
    assert original["status"] == "success", original
    cached = acquire_source(run, request, package=package, max_raster_pixels=100)
    assert cached["cache_hit"]
    open_raster = rasterio.open
    inspected = []

    class HeaderOnly:
        def __init__(self, path, *args, **kwargs):
            self.ds = open_raster(path, *args, **kwargs)
            inspected.append(str(path))

        def __getattr__(self, name):
            return getattr(self.ds, name)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.ds.close()

        def read(self, *args, **kwargs):
            raise AssertionError("oversized raster read before dimension check")

    with patch("rasterio.open", side_effect=HeaderOnly), \
            patch("segment_mapping.sources.fetch_file") as fetch_file, patch("segment_mapping.sources.http") as http:
        result = acquire_source(run, request, package=package, max_raster_pixels=1)
    assert result["status"] == "failed", result
    assert "pixel limit" in result["error"]
    assert not result["cache_hit"]
    assert str(run.path(original["source"]["file"])) in inspected
    assert str(source) in inspected
    fetch_file.assert_not_called()
    http.assert_not_called()


@pytest.mark.parametrize("kind", ["wcs", "arcgis", "file"])
def test_bounded_streams_still_produce_verified_outputs(tmp_path, kind):
    run = make_run(tmp_path)
    if kind == "arcgis":
        package = arcgis_package()
        bodies = [b'{"name":"roads"}', b'{"objectIds":[1]}', json.dumps(dict(
            spatialReference=dict(wkid=31254), features=[dict(
                attributes=dict(OBJECTID=1), geometry=dict(x=50, y=50))])).encode()]
        role = "roads"
    else:
        package = wcs_package()
        bodies = [raster_bytes()]
        role = "dem"
        if kind == "file":
            package = {"acquisition": {"providers": [dict(
                id="terrain", name="dem", role=role, type="file", url="https://official.example/dem.tif")]}}
    responses = [streamed_response(body) for body in bodies]
    with patch("segment_mapping.sources.http", side_effect=responses) as http:
        result = acquire_source(run, dict(role=role, query="Village"), package=package,
                                allow_network=True, max_download_bytes=sum(map(len, bodies)),
                                max_raster_pixels=100)
    assert result["status"] == "success", result
    assert result["download_bytes"] == sum(map(len, bodies))
    assert all(call.kwargs["stream"] for call in http.call_args_list)
    assert all(response.raw.closed for response in responses)


def test_http_error_bodies_share_retry_budget(tmp_path):
    import requests

    first = streamed_response(b"temporarily unavailable", status=503)
    second = streamed_response(b'{"name":"roads"}')
    error = requests.HTTPError("503", response=first)
    with patch("segment_mapping.sources.http", side_effect=[error, second]) as http:
        result = acquire_source(make_run(tmp_path), dict(role="roads", query="Village"),
                                package=arcgis_package(), allow_network=True,
                                max_download_bytes=len(b"temporarily unavailable") + 1, retries=2)
    assert "byte limit" in result["error"], result
    assert len(result["attempts"]) == http.call_count == 2
    assert first.raw.closed and second.raw.closed


def test_redirects_cannot_consume_unaudited_bodies(tmp_path):
    response = streamed_response(b"redirect", status=302)
    response.headers["Location"] = "https://official.example/other"
    with patch("segment_mapping.sources.http", return_value=response) as http:
        result = acquire_source(make_run(tmp_path), dict(role="roads", query="Village"),
                                package=arcgis_package(), allow_network=True)
    assert "redirects are unsupported" in result["error"], result
    assert http.call_args.kwargs["allow_redirects"] is False
    assert http.call_count == 1
    assert response.raw.closed


def test_arcgis_streamed_crs_is_verified_before_writing(tmp_path):
    bodies = [b'{"name":"roads"}', b'{"objectIds":[1]}', json.dumps(dict(
        spatialReference=dict(wkid=4326), features=[dict(
            attributes=dict(OBJECTID=1), geometry=dict(x=50, y=50))])).encode()]
    with patch("segment_mapping.sources.http", side_effect=[streamed_response(body) for body in bodies]):
        result = acquire_source(make_run(tmp_path), dict(role="roads", query="Village"),
                                package=arcgis_package(), allow_network=True)
    assert "refusing relabel" in result["error"], result
