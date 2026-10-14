"""DEM access and terrain models (relative height above channel, elevation masks, D8 watershed)."""
from __future__ import annotations

import heapq

import numpy as np


class Dem:
    def __init__(self, path, name: str = "dem"):
        import rasterio

        self.name = name
        self.path = path
        with rasterio.open(path) as ds:
            z = ds.read(1).astype("float64")
            if ds.nodata is not None and not np.isnan(ds.nodata):
                z[z == ds.nodata] = np.nan
            self.transform = ds.transform
            self.crs = ds.crs
            self.bounds = ds.bounds
        z[(z < -1000) | (z > 9000)] = np.nan
        self.z = z
        self.shape = z.shape
        self.res = abs(self.transform.a)
        self.valid = np.isfinite(z)

    # ------------------------------------------------------------------ indexing
    def index(self, x, y):
        col, row = (~self.transform) @ (x, y)
        return int(np.floor(row)), int(np.floor(col))

    def xy(self, rows, cols):
        x = self.transform.c + (np.asarray(cols) + 0.5) * self.transform.a
        y = self.transform.f + (np.asarray(rows) + 0.5) * self.transform.e
        return x, y

    def inside(self, r, c):
        return 0 <= r < self.shape[0] and 0 <= c < self.shape[1]

    def sample(self, x, y) -> float:
        r, c = self.index(x, y)
        return float(self.z[r, c]) if self.inside(r, c) else float("nan")

    def window_min(self, x, y, radius_m: float) -> float:
        r, c = self.index(x, y)
        k = max(1, int(np.ceil(radius_m / self.res)))
        r0, r1 = max(0, r - k), min(self.shape[0], r + k + 1)
        c0, c1 = max(0, c - k), min(self.shape[1], c + k + 1)
        if r0 >= r1 or c0 >= c1:
            return float("nan")
        rr, cc = np.mgrid[r0:r1, c0:c1]
        xs, ys = self.xy(rr, cc)
        win = self.z[r0:r1, c0:c1]
        use = ((xs - x) ** 2 + (ys - y) ** 2 <= radius_m**2) & np.isfinite(win)
        return float(win[use].min()) if use.any() else float("nan")

    # ------------------------------------------------------------------ raster <-> vector
    def mask(self, geom, all_touched: bool = False) -> np.ndarray:
        from rasterio.features import geometry_mask

        if geom is None or geom.is_empty:
            return np.zeros(self.shape, bool)
        return ~geometry_mask([geom], out_shape=self.shape, transform=self.transform, all_touched=all_touched)

    def polygonize(self, mask: np.ndarray):
        from rasterio.features import shapes
        from shapely.geometry import shape
        from shapely.ops import unary_union

        if not mask.any():
            from shapely.geometry import Polygon

            return Polygon()
        return unary_union([shape(g) for g, v in shapes(mask.astype("uint8"), mask=mask, transform=self.transform) if v == 1])

    def stats(self, geom) -> dict:
        m = self.mask(geom) & self.valid
        if not m.any():
            return dict(cells=0)
        v = self.z[m]
        return dict(cells=int(m.sum()), min=float(v.min()), max=float(v.max()), mean=float(v.mean()))


# ============================================================================ relative height above channel


class ChannelHeightModel:
    """Elevation above the nearest sampled channel bed (not hydraulic HAND, not a flow simulation).

    Bed elevation at each channel sample = minimum DEM value within `bed_window_m` of the line point
    (optionally made monotone so the bed never rises downstream). Cells within `max_dist_m` of the
    channels (and inside `within`) are compared to their nearest bed sample. `geom(h)` keeps cells with
    relative height <= h that are 8-connected to the channel.
    """

    def __init__(self, dem: Dem, lines, max_dist_m: float, within=None, bed_window_m: float = 8.0,
                 step_m: float | None = None, monotone: bool = False, connected: bool = True):
        from scipy.spatial import cKDTree
        from shapely.ops import unary_union

        self.dem = dem
        self.max_dist = max_dist_m
        self.connected = connected
        step = step_m or dem.res
        pts, zs = [], []
        for ln in lines:
            if ln.is_empty:
                continue
            for part in getattr(ln, "geoms", [ln]):
                d = np.r_[np.arange(0, part.length, step), part.length]
                lp = [part.interpolate(float(v)) for v in d]
                z = np.array([dem.window_min(p.x, p.y, bed_window_m) for p in lp])
                if np.isnan(z).all():
                    continue
                if monotone:
                    z = np.where(np.isnan(z), np.nanmax(z), z)
                    if z[0] <= z[-1]:  # line starts at its low end
                        z = np.minimum.accumulate(z[::-1])[::-1]
                    else:
                        z = np.minimum.accumulate(z)
                ok = np.isfinite(z)
                pts += [(p.x, p.y) for p, k in zip(lp, ok) if k]
                zs += list(z[ok])
        if not pts:
            raise ValueError("Channel lines have no DEM coverage")
        zone = unary_union([ln.buffer(max_dist_m, cap_style="flat") for ln in lines])
        if within is not None:
            zone = zone.intersection(within)
        self.zone = zone
        rr, cc = np.where(dem.mask(zone) & dem.valid)
        xs, ys = dem.xy(rr, cc)
        dist, idx = cKDTree(np.array(pts)).query(np.c_[xs, ys])
        self.rr, self.cc, self.dist = rr, cc, dist
        self.rel = dem.z[rr, cc] - np.asarray(zs)[idx]
        self.seed = np.zeros(dem.shape, bool)
        near = dist <= max(bed_window_m, dem.res)
        self.seed[rr[near], cc[near]] = True

    def mask(self, h: float) -> np.ndarray:
        from scipy.ndimage import label

        use = (self.rel <= h) & (self.dist <= self.max_dist)
        m = np.zeros(self.dem.shape, bool)
        m[self.rr[use], self.cc[use]] = True
        if not self.connected:
            return m
        lab, _ = label(m, structure=np.ones((3, 3)))
        keep = np.unique(lab[self.seed & m])
        keep = keep[keep > 0]
        return np.isin(lab, keep) & m

    def geom(self, h: float):
        return self.dem.polygonize(self.mask(h)).intersection(self.zone)


# ============================================================================ D8 watershed

_D8 = [(-1, -1), (-1, 0), (-1, 1), (0, -1), (0, 1), (1, -1), (1, 0), (1, 1)]


class FlowModel:
    """Priority-flood filled DEM (Barnes et al. 2014, epsilon variant) with D8 receivers and accumulation."""

    def __init__(self, dem: Dem):
        z = np.where(dem.valid, dem.z, -1e6)  # nodata drains out
        nr, nc = z.shape
        f = z.copy()
        done = np.zeros(z.shape, bool)
        pq = []
        for r in range(nr):
            for c in (0, nc - 1):
                heapq.heappush(pq, (f[r, c], r, c))
                done[r, c] = True
        for c in range(1, nc - 1):
            for r in (0, nr - 1):
                heapq.heappush(pq, (f[r, c], r, c))
                done[r, c] = True
        eps = 1e-4
        while pq:
            zc, r, c = heapq.heappop(pq)
            for dr, dc in _D8:
                rr, cc = r + dr, c + dc
                if 0 <= rr < nr and 0 <= cc < nc and not done[rr, cc]:
                    done[rr, cc] = True
                    if f[rr, cc] <= zc:
                        f[rr, cc] = zc + eps
                    heapq.heappush(pq, (f[rr, cc], rr, cc))
        self.dem = dem
        self.f = f
        best = np.zeros(f.shape)
        rec = np.full(f.shape, -1, np.int64)
        idx = np.arange(nr * nc).reshape(nr, nc)
        pad = np.pad(f, 1, constant_values=np.inf)
        pidx = np.pad(idx, 1, constant_values=-1)
        for dr, dc in _D8:
            nb = pad[1 + dr : 1 + dr + nr, 1 + dc : 1 + dc + nc]
            drop = (f - nb) / (dem.res * (2**0.5 if dr and dc else 1.0))
            better = drop > best
            best[better] = drop[better]
            rec[better] = pidx[1 + dr : 1 + dr + nr, 1 + dc : 1 + dc + nc][better]
        self.rec = rec.ravel()
        self.order = np.argsort(f.ravel(), kind="stable")
        acc = np.ones(nr * nc)
        recl = self.rec
        for i in self.order[::-1].tolist():
            r = recl[i]
            if r >= 0:
                acc[r] += acc[i]
        self.acc = acc.reshape(f.shape)

    def snap(self, x, y, radius_m: float):
        dem = self.dem
        r, c = dem.index(x, y)
        k = max(0, int(np.ceil(radius_m / dem.res)))
        r0, r1 = max(0, r - k), min(dem.shape[0], r + k + 1)
        c0, c1 = max(0, c - k), min(dem.shape[1], c + k + 1)
        win = self.acc[r0:r1, c0:c1]
        rr, cc = np.unravel_index(np.argmax(win), win.shape)
        return r0 + rr, c0 + cc

    def watershed(self, r: int, c: int) -> np.ndarray:
        nc = self.dem.shape[1]
        lab = np.zeros(self.f.size, bool)
        lab[r * nc + c] = True
        recl = self.rec
        for i in self.order.tolist():
            if not lab[i]:
                t = recl[i]
                if t >= 0 and lab[t]:
                    lab[i] = True
        return lab.reshape(self.f.shape)
