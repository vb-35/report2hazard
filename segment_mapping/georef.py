"""Georeferencing of report figures from ground control points, and colour-class digitizing.

Pixel convention: native image pixels of the figure file, origin upper-left, x to the right, y down,
continuous coordinates (a value read off a grid render is used as-is).
"""
from __future__ import annotations

import ast
import json

import numpy as np

METHODS = {"affine": 3, "similarity": 2, "projective": 4}


class Georef:
    def __init__(self, gid: str, cfg: dict, gcps: list[dict], checks: list[dict]):
        self.id = gid
        self.cfg = cfg
        self.method = cfg.get("method", "affine")
        if self.method not in METHODS:
            raise ValueError(f"georef {gid}: method must be one of {list(METHODS)}")
        if len(gcps) < METHODS[self.method]:
            raise ValueError(f"georef {gid}: {self.method} needs at least {METHODS[self.method]} GCPs, got {len(gcps)}")
        self.gcps, self.checks = gcps, checks
        px = np.array([g["pixel"] for g in gcps], float)
        tg = np.array([g["target"] for g in gcps], float)
        self._fit(px, tg)
        pred = self.apply(px)
        res = np.linalg.norm(pred - tg, axis=1)
        for g, r, p in zip(gcps, res, pred):
            g["predicted"] = [round(float(v), 3) for v in p]
            g["residual_m"] = round(float(r), 3)
        for c in checks:
            p = self.apply(np.array([c["pixel"]], float))[0]
            c["predicted"] = [round(float(v), 3) for v in p]
            c["error_m"] = round(float(np.linalg.norm(p - np.array(c["target"], float))), 3)
        self.rms = float(np.sqrt(np.mean(res**2)))
        self.max_res = float(res.max())
        # ground size of one image pixel (geometric mean of the local scale)
        j = self.apply(np.array([[0, 0], [1, 0], [0, 1]], float))
        u, v = j[1] - j[0], j[2] - j[0]
        self.pixel_size = float(np.sqrt(abs(u[0] * v[1] - u[1] * v[0])))
        if len(gcps) == METHODS[self.method]:
            self.note = "Exactly determined transform: residuals are zero by construction and say nothing about accuracy."
        else:
            self.note = ""

    def _fit(self, px, tg):
        n = len(px)
        if self.method == "affine":
            A = np.c_[px, np.ones(n)]
            self.M = np.linalg.lstsq(A, tg, rcond=None)[0]  # 3x2
        elif self.method == "similarity":
            # image y points down, map y up: reflected similarity X = a x + b y + c ; Y = b x - a y + d
            A = np.zeros((2 * n, 4))
            A[0::2] = np.c_[px[:, 0], px[:, 1], np.ones(n), np.zeros(n)]
            A[1::2] = np.c_[-px[:, 1], px[:, 0], np.zeros(n), np.ones(n)]
            a, b, c, d = np.linalg.lstsq(A, tg.reshape(-1), rcond=None)[0]
            self.M = np.array([[a, b], [b, -a], [c, d]])
        else:
            A = []
            for (x, y), (X, Y) in zip(px, tg):
                A.append([x, y, 1, 0, 0, 0, -X * x, -X * y, -X])
                A.append([0, 0, 0, x, y, 1, -Y * x, -Y * y, -Y])
            _, _, vt = np.linalg.svd(np.array(A))
            self.H = vt[-1].reshape(3, 3) / vt[-1][-1]

    def apply(self, px: np.ndarray) -> np.ndarray:
        px = np.atleast_2d(px)
        if self.method == "projective":
            h = np.c_[px, np.ones(len(px))] @ self.H.T
            return h[:, :2] / h[:, 2:3]
        return np.c_[px, np.ones(len(px))] @ self.M

    def transform_geom(self, g):
        import shapely

        return shapely.transform(g, lambda c: self.apply(c))

    @property
    def rasterio_transform(self):
        from rasterio.transform import Affine

        if self.method == "projective":
            return None
        M = self.M
        return Affine(M[0, 0], M[1, 0], M[2, 0], M[0, 1], M[1, 1], M[2, 1])

    def info(self) -> dict:
        d = dict(id=self.id, figure=self.cfg.get("figure"), image=self.cfg.get("image"), pdf_page=self.cfg.get("pdf_page"),
                 printed_page=self.cfg.get("printed_page"), pixel_convention="native image pixels, origin upper-left, x right, y down",
                 source_crs=self.cfg.get("source_crs", "image pixels (map CRS unknown)"), target_crs=self.cfg.get("target_crs"),
                 transformation=self.method, rms_m=round(self.rms, 3), max_residual_m=round(self.max_res, 3),
                 ground_pixel_size_m=round(self.pixel_size, 3), control_points=self.gcps, independent_checks=self.checks,
                 independent_check_available=bool(self.checks), limitations=self.cfg.get("limitations", ""), note=self.note)
        if self.method == "projective":
            d["matrix_3x3"] = self.H.tolist()
        else:
            d["matrix_3x2"] = self.M.tolist()
        return d


# ============================================================================ colour classification

_ALLOWED = (ast.Expression, ast.BinOp, ast.UnaryOp, ast.Compare, ast.Name, ast.Load, ast.Constant, ast.BitAnd, ast.BitOr,
            ast.Invert, ast.Not, ast.Add, ast.Sub, ast.Mult, ast.Div, ast.USub, ast.Gt, ast.GtE, ast.Lt, ast.LtE, ast.Eq,
            ast.NotEq, ast.Call, ast.BoolOp, ast.And, ast.Or)


def eval_color_rule(rule: str, rgb: np.ndarray) -> np.ndarray:
    """Evaluate e.g. "(R>175)&(G<110)&(B<110)&(R-G>95)" on an RGB array. Names: R,G,B (0-255), H (0-360), S,V (0-100)."""
    from PIL import Image

    tree = ast.parse(rule, mode="eval")
    for node in ast.walk(tree):
        if not isinstance(node, _ALLOWED):
            raise ValueError(f"Colour rule element not allowed: {type(node).__name__}")
        if isinstance(node, ast.Call) and not (isinstance(node.func, ast.Name) and node.func.id == "abs"):
            raise ValueError("Only abs() calls are allowed in colour rules")
        if isinstance(node, ast.Name) and node.id not in ("R", "G", "B", "H", "S", "V", "abs"):
            raise ValueError(f"Unknown name {node.id!r} in colour rule (use R,G,B,H,S,V)")
    hsv = np.asarray(Image.fromarray(rgb.astype("uint8")).convert("HSV")).astype(float)
    env = dict(R=rgb[..., 0].astype(float), G=rgb[..., 1].astype(float), B=rgb[..., 2].astype(float),
               H=hsv[..., 0] * 360 / 255, S=hsv[..., 1] * 100 / 255, V=hsv[..., 2] * 100 / 255, abs=np.abs)
    out = eval(compile(tree, "<rule>", "eval"), {"__builtins__": {}}, env)
    return np.asarray(out, bool)


def color_mask_polygon(image_path, rule: str, exclude_px=None, include_px=None, min_pixels: int = 4, close_px: int = 0,
                       open_px: int = 0):
    """Polygon (in image pixel coordinates) of pixels matching the colour rule."""
    from PIL import Image
    from rasterio.features import shapes
    from rasterio.transform import Affine
    from scipy import ndimage
    from shapely.geometry import shape
    from shapely.ops import unary_union

    rgb = np.asarray(Image.open(image_path).convert("RGB")).astype(int)
    m = eval_color_rule(rule, rgb)
    if include_px:
        x0, y0, x1, y1 = [int(v) for v in include_px]
        keep = np.zeros_like(m)
        keep[y0:y1, x0:x1] = True
        m &= keep
    for box in exclude_px or []:
        x0, y0, x1, y1 = [int(v) for v in box]
        m[y0:y1, x0:x1] = False
    if open_px:
        m = ndimage.binary_opening(m, iterations=int(open_px))
    if close_px:
        m = ndimage.binary_closing(m, iterations=int(close_px))
    if min_pixels > 1:
        lab, n = ndimage.label(m)
        if n:
            sizes = ndimage.sum(m, lab, range(1, n + 1))
            m = np.isin(lab, np.where(sizes >= min_pixels)[0] + 1)
    geoms = [shape(g) for g, v in shapes(m.astype("uint8"), mask=m, transform=Affine.identity()) if v == 1]
    return unary_union(geoms).buffer(0), int(m.sum())


# ============================================================================ QA output


def qa_outputs(run, gr: Georef, layers=("streams", "osm_buildings", "osm_roads", "roads"), extra=None):
    """Warp the figure into the run CRS (tmp only) and plot it with reference layers, GCPs and residuals."""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from PIL import Image

    img_path = run.path(gr.cfg["image"])
    im = np.asarray(Image.open(img_path).convert("RGB"))
    h, w = im.shape[:2]
    corners = gr.apply(np.array([[0, 0], [w, 0], [w, h], [0, h]], float))
    fig, ax = plt.subplots(figsize=(12, 12))
    tr = gr.rasterio_transform
    if tr is not None:
        import rasterio
        from rasterio.warp import Resampling, reproject

        x0, y0 = corners.min(axis=0)
        x1, y1 = corners.max(axis=0)
        res = max(gr.pixel_size, 0.1)
        W, H = int((x1 - x0) / res) + 1, int((y1 - y0) / res) + 1
        dst = np.zeros((3, H, W), np.uint8)
        from rasterio.transform import from_origin

        dtr = from_origin(x0, y1, res, res)
        for b in range(3):
            reproject(im[..., b], dst[b], src_transform=tr, src_crs=run.crs, dst_transform=dtr, dst_crs=run.crs,
                      resampling=Resampling.bilinear)
        out_tif = run.inspect / f"georef_{gr.id}_warped.tif"
        with rasterio.open(out_tif, "w", driver="GTiff", width=W, height=H, count=3, dtype="uint8", crs=run.crs, transform=dtr) as ds:
            ds.write(dst)
        ax.imshow(dst.transpose(1, 2, 0), extent=(x0, x1, y0, y1), alpha=0.75)
    else:
        ax.plot(*np.r_[corners, corners[:1]].T, "k-", lw=1)
    for name in layers:
        try:
            g = run.layer(name)
        except Exception:
            continue
        g = g.cx[corners[:, 0].min() : corners[:, 0].max(), corners[:, 1].min() : corners[:, 1].max()]
        if len(g):
            if g.geom_type.isin(["Polygon", "MultiPolygon"]).all():
                g.boundary.plot(ax=ax, color="cyan", lw=0.6)
            else:
                g.plot(ax=ax, color="blue" if "stream" in name or "water" in name else "orange", lw=1)
    if extra is not None and not extra.is_empty:
        import geopandas as gpd

        gpd.GeoSeries([extra], crs=run.crs).boundary.plot(ax=ax, color="red", lw=1)
    for g in gr.gcps:
        ax.plot(*g["target"], "b+", ms=12)
        ax.plot(*g["predicted"], "rx", ms=8)
        ax.plot([g["target"][0], g["predicted"][0]], [g["target"][1], g["predicted"][1]], "r-", lw=1)
        ax.annotate(f"{g.get('label', '')} {g['residual_m']:.1f} m", g["target"], fontsize=7, color="blue")
    for c in gr.checks:
        ax.plot(*c["target"], "g+", ms=14)
        ax.plot(*c["predicted"], "gx", ms=8)
        ax.annotate(f"CHECK {c.get('label', '')} {c['error_m']:.1f} m", c["target"], fontsize=7, color="green")
    ax.set_xlim(corners[:, 0].min(), corners[:, 0].max())
    ax.set_ylim(corners[:, 1].min(), corners[:, 1].max())
    ax.set_aspect("equal")
    ax.set_title(f"{gr.id}: {gr.method}, RMS {gr.rms:.2f} m, max {gr.max_res:.2f} m, pixel {gr.pixel_size:.2f} m")
    target = run.inspect / f"georef_{gr.id}_qa.png"
    fig.savefig(target, dpi=130, bbox_inches="tight")
    plt.close(fig)
    return target


def dump(georefs: dict) -> str:
    return json.dumps({k: v.info() for k, v in georefs.items()}, ensure_ascii=False, indent=2)
