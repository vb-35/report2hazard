/* 3D terrain view: a lit elevation mesh with a basemap and part fills draped over it,
   terrain-following outlines, and camera-facing labels at a constant screen size. One run is shown at a time.
   Needs an import map for "three" (see the page templates). */
import * as THREE from "three";
import { OrbitControls } from "three/addons/controls/OrbitControls.js";
import { Line2 } from "three/addons/lines/Line2.js";
import { LineGeometry } from "three/addons/lines/LineGeometry.js";
import { LineMaterial } from "three/addons/lines/LineMaterial.js";
import { partStyle, geometryAnchor, northArrow, EOX_URL, EOX_ATTR } from "./map-display.js";

const TILE_URLS = {
  eox: EOX_URL,
  osm: "https://tile.openstreetmap.org/{z}/{x}/{y}.png",
  grau: "https://mapsneu.wien.gv.at/basemap/bmapgrau/normal/google3857/{z}/{y}/{x}.png",
  ortho: "https://mapsneu.wien.gv.at/basemap/bmaporthofoto30cm/normal/google3857/{z}/{y}/{x}.jpeg",
};
const TEXTURE_PX = 2048;
const MAX_TILES = 144;

export async function mount(container, options = {}) {
  const body = options.file ? new FormData() : null;
  if (body) body.append("dem", options.file);
  const response = await fetch(options.url, { method: body ? "POST" : "GET", body, headers: { Accept: "application/json" } });
  const data = await response.json().catch(() => ({}));
  if (response.headers.get("X-Terrain-Version") !== "2") {
    throw new Error("The interface server is still running an older version. Close its server window, reopen Start Interface.cmd, then reload this page.");
  }
  if (!response.ok) throw new Error(data.error || "The 3D view could not be loaded.");
  const source = new URL(options.url, location.href).searchParams.get("source") || "run";
  if (data.terrain?.source !== source) throw new Error("The server did not load the selected terrain source. Reload the interface and try again.");
  return new TerrainView(container, data, options);
}

// Shared source choices for single-run and comparison views; remain usable after a load failure.
export function terrainSources(host, url, load) {
  host.innerHTML = `<div class="terrain3d-tools">
    <button type="button" class="button secondary" data-source="run">Use run DEM</button>
    <button type="button" class="button secondary" data-source="online">Retrieve online terrain</button>
    <label>Elevation GeoTIFF <input type="file" accept=".tif,.tiff" aria-label="Elevation GeoTIFF"></label>
    <button type="button" class="button secondary" data-source="upload">Upload DEM</button>
    </div><p class="hint" role="status" aria-live="polite"></p>`;
  const status = host.querySelector('[role="status"]');
  let generation = 0, current = null;
  async function run(source, file) {
    const token = ++generation;
    host.querySelectorAll('button').forEach(b => b.disabled = true);
    status.textContent = source === 'online' ? 'Retrieving online terrain (first load may take a minute)…' : 'Loading elevation model…';
    const target = new URL(url, location.href); target.searchParams.set('source', source);
    try {
      if (current) { current.dispose(); current = null; }
      const view = await load({ url: target.href, file });
      if (token !== generation) { view.dispose(); return; }
      current = view;
      status.textContent = view.data.terrain.attribution || 'Elevation model loaded.';
    } catch (err) {
      if (token === generation) status.textContent = err.message || String(err);
    } finally {
      if (token === generation) host.querySelectorAll('button').forEach(b => b.disabled = false);
    }
  }
  host.querySelectorAll('[data-source]').forEach(b => b.addEventListener('click', () => {
    const file = host.querySelector('input').files[0];
    if (b.dataset.source === 'upload' && !file) { status.textContent = 'Choose an elevation GeoTIFF first.'; return; }
    run(b.dataset.source, b.dataset.source === 'upload' ? file : null);
  }));
  host.querySelector('input').addEventListener('change', e => {
    status.textContent = e.target.files[0] ? `Selected: ${e.target.files[0].name}. Click Upload DEM.` : 'No DEM selected.';
  });
  run('run');
}

const tile2lon = (x, z) => x / 2 ** z * 360 - 180;
const tile2lat = (y, z) => { const n = Math.PI - 2 * Math.PI * y / 2 ** z; return 180 / Math.PI * Math.atan(Math.sinh(n)); };
const lon2tile = (lon, z) => (lon + 180) / 360 * 2 ** z;
const lat2tile = (lat, z) => { const r = lat * Math.PI / 180; return (1 - Math.log(Math.tan(r) + 1 / Math.cos(r)) / Math.PI) / 2 * 2 ** z; };

class TerrainView {
  constructor(container, data, options) {
    this.container = container;
    this.data = data;
    this.run = options.run && data.runs[options.run] ? options.run : data.models[0].key;
    this.segments = new Set(options.segments ?? (options.segment ? [options.segment] : data.segments.map(s => s.id)));
    this.basemap = options.basemap || "ortho";
    this.exaggeration = options.exaggeration || 1.5;
    this.labels = options.labels !== false;
    this.context = options.context !== false;
    this.others = !!options.others;
    this.tiles = new Map();
    const t = data.terrain;
    [this.x0, this.y0, this.x1, this.y1] = t.bounds;
    this.W = this.x1 - this.x0;
    this.H = this.y1 - this.y0;

    this.canvas = document.createElement("canvas");
    const longSide = Math.max(this.W, this.H);
    this.canvas.width = Math.round(TEXTURE_PX * this.W / longSide);
    this.canvas.height = Math.round(TEXTURE_PX * this.H / longSide);
    this.ctx = this.canvas.getContext("2d");
    this.shade = this.hillshade();

    this.renderer = new THREE.WebGLRenderer({ antialias: true });
    this.renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
    container.innerHTML = "";
    container.append(this.renderer.domElement);
    this.labelLayer = document.createElement("div");
    this.labelLayer.className = "terrain-label-layer";
    container.append(this.labelLayer);
    this.compass = northArrow(container);
    this.attribution = document.createElement("div");
    this.attribution.className = "terrain-attribution";
    container.append(this.attribution);
    this.anchors = new WeakMap();
    this.overlayDirty = true;
    this.scene = new THREE.Scene();
    this.overlays = new THREE.Group();
    this.scene.add(this.overlays);
    this.scene.background = new THREE.Color("#dfe7ef");
    this.scene.add(new THREE.HemisphereLight(0xffffff, 0x8a8f99, 2.2));
    const sun = new THREE.DirectionalLight(0xffffff, 2.4);
    sun.position.set(-longSide, longSide * 1.2, -longSide * 0.6);  // from the north-west, like a hillshade
    this.scene.add(sun);

    this.geometry = new THREE.PlaneGeometry(this.W, this.H, t.nx - 1, t.ny - 1);
    this.texture = new THREE.CanvasTexture(this.canvas);
    this.texture.colorSpace = THREE.SRGBColorSpace;
    this.texture.anisotropy = this.renderer.capabilities.getMaxAnisotropy();
    this.mesh = new THREE.Mesh(this.geometry, new THREE.MeshLambertMaterial({ map: this.texture, side: THREE.DoubleSide, polygonOffset: true, polygonOffsetFactor: 1, polygonOffsetUnits: 1 }));
    this.mesh.rotation.x = -Math.PI / 2;
    this.scene.add(this.mesh);
    this.applyHeights();

    this.camera = new THREE.PerspectiveCamera(40, 1, 5, longSide * 20);
    const relief = (t.zmax - t.zmin) * this.exaggeration;
    this.camera.position.set(-longSide * 0.35, relief + longSide * 0.55, longSide * 0.85);
    this.controls = new OrbitControls(this.camera, this.renderer.domElement);
    this.controls.target.set(0, relief * 0.35, 0);
    this.controls.maxPolarAngle = Math.PI * 0.495;
    this.controls.enableDamping = true;
    this.controls.screenSpacePanning = true;
    this.controls.saveState();

    this.resize = () => {
      const width = container.clientWidth || 600, height = container.clientHeight || 520;
      this.renderer.setSize(width, height);
      this.camera.aspect = width / height;
      this.camera.updateProjectionMatrix();
    };
    this.observer = new ResizeObserver(this.resize);
    this.observer.observe(container);
    this.resize();
    this.redraw();
    this.loadTiles();
    const loop = () => {
      this.frame = requestAnimationFrame(loop);
      this.controls.update();
      this.updateOverlayPositions();
      this.renderer.render(this.scene, this.camera);
    };
    loop();
  }

  model() { return this.data.models.find(m => m.key === this.run); }

  applyHeights() {
    const { z, zmin } = this.data.terrain, pos = this.geometry.attributes.position;
    for (let i = 0; i < pos.count; i++) pos.setZ(i, (z[i] - zmin) * this.exaggeration);
    pos.needsUpdate = true;
    this.geometry.computeVertexNormals();
    this.overlayDirty = true;
  }

  /* Elevation tint and hillshade at DEM resolution, the backdrop when no basemap is shown or tiles are missing. */
  hillshade() {
    const { nx, ny, z, zmin, zmax } = this.data.terrain;
    const off = document.createElement("canvas");
    off.width = nx; off.height = ny;
    const img = off.getContext("2d").createImageData(nx, ny);
    const dx = this.W / (nx - 1), dy = this.H / (ny - 1), range = Math.max(1, zmax - zmin);
    const az = 315 * Math.PI / 180, alt = 45 * Math.PI / 180;
    const at = (c, r) => z[Math.min(ny - 1, Math.max(0, r)) * nx + Math.min(nx - 1, Math.max(0, c))];
    const ramp = [[188, 208, 168], [206, 190, 156], [236, 236, 236]];
    for (let r = 0; r < ny; r++) for (let c = 0; c < nx; c++) {
      const gx = (at(c + 1, r) - at(c - 1, r)) / (2 * dx), gy = (at(c, r - 1) - at(c, r + 1)) / (2 * dy);
      const slope = Math.atan(Math.hypot(gx, gy)), aspect = Math.atan2(gy, -gx);
      const shade = Math.max(0, Math.sin(alt) * Math.cos(slope) + Math.cos(alt) * Math.sin(slope) * Math.cos(az - aspect));
      const h = (at(c, r) - zmin) / range, k = h < .5 ? h * 2 : (h - .5) * 2, a = ramp[h < .5 ? 0 : 1], b = ramp[h < .5 ? 1 : 2];
      const f = 0.55 + 0.6 * shade, i = 4 * (r * nx + c);
      for (let j = 0; j < 3; j++) img.data[i + j] = Math.min(255, (a[j] + (b[j] - a[j]) * k) * f);
      img.data[i + 3] = 255;
    }
    off.getContext("2d").putImageData(img, 0, 0);
    return off;
  }

  toCanvas(x, y) { return [(x - this.x0) / this.W * this.canvas.width, (this.y1 - y) / this.H * this.canvas.height]; }

  lonlatToCanvas(lon, lat) {
    const [a, b, c, d, e, f] = this.data.terrain.lonlat_to_xy;
    return this.toCanvas(a * lon + b * lat + c, d * lon + e * lat + f);
  }

  tileZoom() {
    const [w, s, e, n] = this.data.terrain.lonlat_bounds;
    let z = Math.min(18, Math.max(1, Math.round(Math.log2(this.canvas.width / 256 * 360 / (e - w)))));
    while (z > 1 && (Math.floor(lon2tile(e, z)) - Math.floor(lon2tile(w, z)) + 1) * (Math.floor(lat2tile(s, z)) - Math.floor(lat2tile(n, z)) + 1) > MAX_TILES) z--;
    return Math.min(z, this.basemap === "eox" ? 14 : 18);
  }

  loadTiles() {
    const kind = this.basemap;
    this.attribution.innerHTML = kind === "eox" ? EOX_ATTR : kind === "osm" ? '&copy; OpenStreetMap contributors' : kind === "none" ? '' : '<a href="https://basemap.at/">basemap.at</a>';
    if (!TILE_URLS[kind]) return;
    const [w, s, e, n] = this.data.terrain.lonlat_bounds, z = this.tileZoom();
    for (let x = Math.floor(lon2tile(w, z)); x <= Math.floor(lon2tile(e, z)); x++) {
      for (let y = Math.floor(lat2tile(n, z)); y <= Math.floor(lat2tile(s, z)); y++) {
        const key = `${kind}/${z}/${x}/${y}`;
        if (this.tiles.has(key)) continue;
        const img = new Image();
        img.crossOrigin = "anonymous";  // tiles without CORS fail to load instead of blocking the texture
        const entry = { img, z, x, y, kind, ok: false };
        img.onload = () => { entry.ok = true; this.redrawSoon(); };
        img.src = TILE_URLS[kind].replace("{z}", z).replace("{x}", x).replace("{y}", y);
        this.tiles.set(key, entry);
      }
    }
  }

  redrawSoon() {
    if (this.pending) return;
    this.pending = setTimeout(() => { this.pending = null; this.redraw(); }, 120);
  }

  drawTile(t) {
    const [tlx, tly] = this.lonlatToCanvas(tile2lon(t.x, t.z), tile2lat(t.y, t.z));
    const [trx, try_] = this.lonlatToCanvas(tile2lon(t.x + 1, t.z), tile2lat(t.y, t.z));
    const [blx, bly] = this.lonlatToCanvas(tile2lon(t.x, t.z), tile2lat(t.y + 1, t.z));
    const ctx = this.ctx;
    ctx.setTransform((trx - tlx) / 256, (try_ - tly) / 256, (blx - tlx) / 256, (bly - tly) / 256, tlx, tly);
    ctx.drawImage(t.img, -0.5, -0.5, 257, 257);  // overlap half a pixel to hide seams
    ctx.setTransform(1, 0, 0, 1, 0, 0);
  }

  path(geometry) {
    const ctx = this.ctx;
    const ring = coords => coords.forEach((p, i) => { const [u, v] = this.toCanvas(p[0], p[1]); i ? ctx.lineTo(u, v) : ctx.moveTo(u, v); });
    const g = geometry, c = g.coordinates;
    ctx.beginPath();
    if (g.type === "Polygon") c.forEach(r => { ring(r); ctx.closePath(); });
    else if (g.type === "MultiPolygon") c.forEach(p => p.forEach(r => { ring(r); ctx.closePath(); }));
    else if (g.type === "LineString") ring(c);
    else if (g.type === "MultiLineString") c.forEach(ring);
  }

  points(geometry) {
    const g = geometry;
    return g.type === "Point" ? [g.coordinates] : g.type === "MultiPoint" ? g.coordinates : [];
  }

  worldPoint([x, y]) {
    const t = this.data.terrain;
    const col = Math.max(0, Math.min(t.nx - 1, (x - this.x0) / this.W * (t.nx - 1)));
    const row = Math.max(0, Math.min(t.ny - 1, (this.y1 - y) / this.H * (t.ny - 1)));
    const c = Math.floor(col), r = Math.floor(row), u = col - c, v = row - r;
    const at = (i, j) => t.z[Math.min(t.ny - 1, j) * t.nx + Math.min(t.nx - 1, i)];
    // Match PlaneGeometry's two triangles per cell, rather than a bilinear surface.
    const z = u + v <= 1
      ? at(c, r) * (1 - u - v) + at(c + 1, r) * u + at(c, r + 1) * v
      : at(c + 1, r + 1) * (u + v - 1) + at(c + 1, r) * (1 - v) + at(c, r + 1) * (1 - u);
    return new THREE.Vector3(x - (this.x0 + this.x1) / 2, (z - t.zmin) * this.exaggeration + 2, (this.y0 + this.y1) / 2 - y);
  }

  clearOverlays() {
    for (const line of [...this.overlays.children]) {
      line.geometry.dispose(); line.material.dispose(); this.overlays.remove(line);
    }
    this.labelLayer.replaceChildren();
    this.screenItems = [];
  }

  drawOutline(geometry, symbol, order = 10) {
    const g = geometry, c = g.coordinates;
    const paths = g.type === "Polygon" ? c : g.type === "MultiPolygon" ? c.flat() :
      g.type === "LineString" ? [c] : g.type === "MultiLineString" ? c : [];
    const gridPoint = p => [(p[0] - this.x0) / this.W * (this.data.terrain.nx - 1),
      (this.y1 - p[1]) / this.H * (this.data.terrain.ny - 1)];
    // Clip context and part outlines to the actual terrain footprint before draping.
    const clipped = [];
    for (const path of paths) {
      let current = [];
      for (let i = 1; i < path.length; i++) {
        const a = path[i - 1], b = path[i], dx = b[0] - a[0], dy = b[1] - a[1];
        let lo = 0, hi = 1, visible = true;
        for (const [p, q] of [[-dx, a[0] - this.x0], [dx, this.x1 - a[0]], [-dy, a[1] - this.y0], [dy, this.y1 - a[1]]]) {
          if (p === 0) { if (q < 0) visible = false; continue; }
          const t = q / p;
          if (p < 0) lo = Math.max(lo, t); else hi = Math.min(hi, t);
        }
        if (!visible || lo > hi) { if (current.length) clipped.push(current); current = []; continue; }
        const start = [a[0] + lo * dx, a[1] + lo * dy], end = [a[0] + hi * dx, a[1] + hi * dy];
        if (current.length && Math.hypot(start[0] - current.at(-1)[0], start[1] - current.at(-1)[1]) > .01) { clipped.push(current); current = []; }
        if (!current.length) current.push(start);
        current.push(end);
      }
      if (current.length) clipped.push(current);
    }
    for (const path of clipped) {
      if (path.length < 2) continue;
      const positions = [];
      path.forEach((p, i) => {
        if (i) {
          const a = path[i - 1], ga = gridPoint(a), gb = gridPoint(p), cuts = new Set();
          // Split at every mesh edge, including the diagonal of each cell. Each resulting
          // line segment lies on one terrain triangle, so it cannot cut through a ridge.
          for (const [start, end] of [[ga[0], gb[0]], [ga[1], gb[1]], [ga[0] + ga[1], gb[0] + gb[1]]]) {
            if (start === end) continue;
            for (let k = Math.floor(Math.min(start, end)) + 1; k < Math.max(start, end); k++) {
              const t = (k - start) / (end - start);
              if (t > 0 && t < 1) cuts.add(t);
            }
          }
          for (const t of [...cuts].sort((a, b) => a - b)) positions.push(...this.worldPoint([a[0] + (p[0] - a[0]) * t, a[1] + (p[1] - a[1]) * t]).toArray());
        }
        positions.push(...this.worldPoint(p).toArray());
      });
      for (const halo of [true, false]) {
        const dash = symbol.dashArray?.split(" ").map(Number);
        const material = new LineMaterial({ color: halo ? "#fff" : symbol.color,
          linewidth: symbol.weight + (halo ? (symbol.haloWidth ?? 3) : 0), opacity: symbol.opacity * (halo ? .9 : 1),
          transparent: true, depthTest: true, depthWrite: false, dashed: !!dash,
          dashSize: dash?.[0] ?? 1, gapSize: dash?.[1] ?? 1 });
        const line = new Line2(new LineGeometry().setPositions(positions), material);
        line.computeLineDistances(); line.renderOrder = order + (halo ? 0 : 1); line.frustumCulled = false;
        this.overlays.add(line);
      }
    }
  }

  rebuildOverlays() {
    this.clearOverlays();
    if (this.context) {
      for (const f of this.data.context.streams || []) this.drawOutline(f.geometry, { color: "#1971c2", weight: 1, haloWidth: .6, opacity: 1 }, 2);
      for (const f of this.data.context.catchment || []) this.drawOutline(f.geometry, { color: "#1d2433", weight: 1.8, opacity: .85, dashArray: "5 4" }, 4);
    }
    const features = [...this.data.runs[this.run].features].sort((a, b) => this.segments.has(a.properties.seg) - this.segments.has(b.properties.seg));
    for (const f of features) {
      const own = this.segments.has(f.properties.seg), p = f.properties;
      if (!own && !this.others) continue;
      const symbol = partStyle(p, this.model().color, !own);
      this.drawOutline(f.geometry, symbol, own ? 10 : 6);
      const add = (point, text) => {
        if (point[0] < this.x0 || point[0] > this.x1 || point[1] < this.y0 || point[1] > this.y1) return;
        const el = document.createElement("span");
        el.className = text ? "terrain-part-label" : "terrain-point";
        if (!text && (p.hypo || p.role === "search_area")) el.classList.add(p.hypo ? "hypothesis" : "search-area");
        if (!own) el.style.opacity = ".4";
        el.style.setProperty("--part-color", symbol.color);
        el.textContent = text || "";
        el.title = `Segment ${p.seg} · ${p.part || p.seg} · ${p.label || p.role}`;
        this.labelLayer.append(el);
        this.screenItems.push({ el, point: this.worldPoint(point), label: !!text, width: text ? el.offsetWidth : 13 });
      };
      for (const point of this.points(f.geometry)) add(point);
      if (own && this.labels) {
        if (!this.anchors.has(f.geometry)) this.anchors.set(f.geometry, geometryAnchor(f.geometry));
        add(this.anchors.get(f.geometry), String(p.part || p.seg));
      }
    }
    this.overlayDirty = false;
  }

  updateOverlayPositions() {
    if (this.overlayDirty) this.rebuildOverlays();
    const width = this.container.clientWidth, height = this.container.clientHeight;
    const pixelScale = 2 * this.camera.position.distanceTo(this.controls.target) * Math.tan(THREE.MathUtils.degToRad(this.camera.fov / 2)) / height;
    for (const line of this.overlays.children) {
      line.material.resolution.set(width, height);
      if (line.material.dashed) line.material.dashScale = 1 / pixelScale;
    }
    const occupied = [];
    this.camera.updateMatrixWorld();
    const affine = this.data.terrain.lonlat_to_xy;
    const north = new THREE.Vector3(affine[1], 0, -affine[4]).transformDirection(this.camera.matrixWorldInverse);
    const bearing = Math.atan2(north.x, north.y) * 180 / Math.PI;
    this.compass.style.setProperty("--north-bearing", `${bearing}deg`);
    for (const item of this.screenItems) {
      const p = item.point.clone().project(this.camera);
      const x = (p.x + 1) * width / 2, y = (1 - p.y) * height / 2;
      item.el.hidden = p.z < -1 || p.z > 1 || x < 0 || x > width || y < 0 || y > height;
      if (item.el.hidden) continue;
      let left = x - item.width / 2, top = y - 6;
      if (item.label) {
        const offsets = [-32, 8, -60, 36];
        const offset = offsets.find(d => {
          const box = [left, y + d, left + item.width, y + d + 24];
          return box[0] >= 2 && box[2] <= width - 2 && box[1] >= 2 && box[3] <= height - 2 &&
            occupied.every(b => box[2] + 4 < b[0] || box[0] > b[2] + 4 || box[3] + 3 < b[1] || box[1] > b[3] + 3);
        });
        if (offset === undefined) { item.el.hidden = true; continue; }
        top = y + offset;
        occupied.push([left, top, left + item.width, top + 24]);
      }
      item.el.style.transform = `translate(${Math.round(left)}px, ${Math.round(top)}px)`;
    }
  }

  redraw() {
    const ctx = this.ctx, cw = this.canvas.width, ch = this.canvas.height;
    const scale = cw / this.W;  // canvas pixels per metre
    ctx.setTransform(1, 0, 0, 1, 0, 0);
    ctx.imageSmoothingEnabled = true;
    ctx.drawImage(this.shade, 0, 0, cw, ch);
    for (const t of this.tiles.values()) if (t.ok && t.kind === this.basemap) this.drawTile(t);
    const px = metres => Math.max(1.5, metres * scale);
    if (this.context) {
      ctx.lineJoin = ctx.lineCap = "round";
      for (const f of this.data.context.roads || []) { this.path(f.geometry); ctx.strokeStyle = "rgba(90,90,90,.7)"; ctx.lineWidth = px(5); ctx.stroke(); }
    }
    const model = this.model(), run = this.data.runs[this.run];
    const mine = f => this.segments.has(f.properties.seg);
    const ordered = [...run.features].sort((a, b) => mine(a) - mine(b));  // the selected segment on top
    for (const f of ordered) {
      const p = f.properties, own = mine(f);
      if (!own && !this.others) continue;
      const symbol = partStyle(p, model.color, !own), color = symbol.color;
      ctx.globalAlpha = own ? 1 : 0.55;
      this.path(f.geometry);
      if (f.geometry.type.endsWith("Polygon")) {
        ctx.fillStyle = color;
        ctx.globalAlpha = symbol.fillOpacity;
        ctx.fill("evenodd");
        ctx.globalAlpha = own ? 1 : 0.55;
      }
      ctx.globalAlpha = 1;
    }
    this.texture.needsUpdate = true;
  }

  setRun(key) { if (this.data.runs[key]) { this.run = key; this.overlayDirty = true; this.redraw(); } }
  setSegments(segments) { this.segments = new Set(segments); this.overlayDirty = true; this.redraw(); }
  setSegment(seg) { this.setSegments(seg ? [seg] : this.data.segments.map(s => s.id)); }
  setBasemap(kind) { this.basemap = kind; this.loadTiles(); this.redraw(); }
  setExaggeration(value) { this.exaggeration = Number(value) || 1; this.applyHeights(); }
  setOptions({ labels, context, others } = {}) {
    if (labels !== undefined) this.labels = labels;
    if (context !== undefined) this.context = context;
    if (others !== undefined) this.others = others;
    this.overlayDirty = true; this.redraw();
  }
  resetView() { this.controls.reset(); }
  dispose() {
    cancelAnimationFrame(this.frame);
    clearTimeout(this.pending);
    for (const { img } of this.tiles.values()) img.onload = null;
    this.clearOverlays();
    this.observer.disconnect();
    this.controls.dispose();
    this.geometry.dispose(); this.texture.dispose(); this.mesh.material.dispose();
    this.renderer.dispose();
    this.container.innerHTML = "";
  }
}
