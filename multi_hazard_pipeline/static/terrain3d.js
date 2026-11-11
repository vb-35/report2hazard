/* 3D terrain view of a localization: the run's elevation model as a lit mesh, with the basemap (or a hillshade),
   the catchment, streams and the run's parts drawn onto the texture draped over it. One run is shown at a time.
   Needs an import map for "three" (see the page templates). */
import * as THREE from "three";
import { OrbitControls } from "three/addons/controls/OrbitControls.js";

const TILE_URLS = {
  osm: "https://tile.openstreetmap.org/{z}/{x}/{y}.png",
  grau: "https://mapsneu.wien.gv.at/basemap/bmapgrau/normal/google3857/{z}/{y}/{x}.png",
  ortho: "https://mapsneu.wien.gv.at/basemap/bmaporthofoto30cm/normal/google3857/{z}/{y}/{x}.jpeg",
};
const TEXTURE_PX = 2048;
const MAX_TILES = 144;

export async function mount(container, options = {}) {
  const response = await fetch(options.url, { headers: { Accept: "application/json" } });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.error || "The 3D view could not be loaded.");
  return new TerrainView(container, data, options);
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
    this.segment = options.segment || null;
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
    this.scene = new THREE.Scene();
    this.scene.background = new THREE.Color("#dfe7ef");
    this.scene.add(new THREE.HemisphereLight(0xffffff, 0x8a8f99, 2.2));
    const sun = new THREE.DirectionalLight(0xffffff, 2.4);
    sun.position.set(-longSide, longSide * 1.2, -longSide * 0.6);  // from the north-west, like a hillshade
    this.scene.add(sun);

    this.geometry = new THREE.PlaneGeometry(this.W, this.H, t.nx - 1, t.ny - 1);
    this.texture = new THREE.CanvasTexture(this.canvas);
    this.texture.colorSpace = THREE.SRGBColorSpace;
    this.texture.anisotropy = this.renderer.capabilities.getMaxAnisotropy();
    this.mesh = new THREE.Mesh(this.geometry, new THREE.MeshLambertMaterial({ map: this.texture }));
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
    const loop = () => { this.frame = requestAnimationFrame(loop); this.controls.update(); this.renderer.render(this.scene, this.camera); };
    loop();
  }

  model() { return this.data.models.find(m => m.key === this.run); }

  applyHeights() {
    const { z, zmin } = this.data.terrain, pos = this.geometry.attributes.position;
    for (let i = 0; i < pos.count; i++) pos.setZ(i, (z[i] - zmin) * this.exaggeration);
    pos.needsUpdate = true;
    this.geometry.computeVertexNormals();
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
    return z;
  }

  loadTiles() {
    const kind = this.basemap;
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

  anchor(geometry) {
    const g = geometry;
    const first = g.type === "Point" ? [g.coordinates] : g.type === "Polygon" ? g.coordinates[0]
      : g.type === "MultiPolygon" ? g.coordinates[0][0] : g.type === "LineString" ? g.coordinates
      : g.type === "MultiLineString" ? g.coordinates[0] : g.coordinates;
    if (g.type === "LineString" || g.type === "MultiLineString") return first[Math.floor(first.length / 2)];
    const sum = first.reduce((s, p) => [s[0] + p[0], s[1] + p[1]], [0, 0]);
    return [sum[0] / first.length, sum[1] / first.length];
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
      for (const f of this.data.context.streams || []) { this.path(f.geometry); ctx.strokeStyle = "#1971c2"; ctx.lineWidth = px(6); ctx.stroke(); }
      for (const f of this.data.context.catchment || []) {
        this.path(f.geometry); ctx.setLineDash([px(30), px(20)]); ctx.strokeStyle = "#111"; ctx.lineWidth = px(5); ctx.stroke(); ctx.setLineDash([]);
      }
    }
    const model = this.model(), run = this.data.runs[this.run];
    const mine = f => !this.segment || f.properties.seg === this.segment;
    const ordered = [...run.features].sort((a, b) => mine(a) - mine(b));  // the selected segment on top
    const labels = [];
    for (const f of ordered) {
      const p = f.properties, own = mine(f);
      if (!own && !this.others) continue;
      const color = own ? model.color : "#868e96";
      ctx.globalAlpha = own ? 1 : 0.55;
      this.path(f.geometry);
      if (f.geometry.type.endsWith("Polygon")) {
        ctx.fillStyle = color;
        ctx.globalAlpha = own ? (p.role === "search_area" ? 0.14 : 0.38) : 0.12;
        ctx.fill("evenodd");
        ctx.globalAlpha = own ? 1 : 0.55;
      }
      ctx.setLineDash(p.role === "search_area" ? [px(6), px(14)] : p.hypo ? [px(22), px(14)] : []);
      ctx.strokeStyle = color;
      ctx.lineWidth = px(f.geometry.type.includes("Line") ? 14 : 7);
      ctx.stroke();
      ctx.setLineDash([]);
      for (const [x, y] of this.points(f.geometry)) {
        const [u, v] = this.toCanvas(x, y);
        ctx.beginPath(); ctx.arc(u, v, px(28), 0, 2 * Math.PI);
        ctx.fillStyle = color; ctx.fill(); ctx.lineWidth = px(6); ctx.strokeStyle = "#fff"; ctx.stroke();
      }
      ctx.globalAlpha = 1;
      if (own && this.labels) labels.push([this.anchor(f.geometry), p.part || p.seg]);
    }
    ctx.font = `700 ${Math.round(Math.max(14, cw / 110))}px system-ui, sans-serif`;
    ctx.textAlign = "center"; ctx.textBaseline = "middle";
    for (const [[x, y], text] of labels) {
      const [u, v] = this.toCanvas(x, y);
      ctx.lineWidth = 5; ctx.strokeStyle = "rgba(255,255,255,.92)"; ctx.strokeText(text, u, v);
      ctx.fillStyle = "#1d2433"; ctx.fillText(text, u, v);
    }
    this.texture.needsUpdate = true;
  }

  setRun(key) { if (this.data.runs[key]) { this.run = key; this.redraw(); } }
  setSegment(seg) { this.segment = seg || null; this.redraw(); }
  setBasemap(kind) { this.basemap = kind; this.loadTiles(); this.redraw(); }
  setExaggeration(value) { this.exaggeration = Number(value) || 1; this.applyHeights(); }
  setOptions({ labels, context, others } = {}) {
    if (labels !== undefined) this.labels = labels;
    if (context !== undefined) this.context = context;
    if (others !== undefined) this.others = others;
    this.redraw();
  }
  resetView() { this.controls.reset(); }
  dispose() {
    cancelAnimationFrame(this.frame);
    this.observer.disconnect();
    this.controls.dispose();
    this.geometry.dispose(); this.texture.dispose(); this.mesh.material.dispose();
    this.renderer.dispose();
    this.container.innerHTML = "";
  }
}
