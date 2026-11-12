/* Shared symbol rules for the 2D and 3D viewers and their legends. */
export const SYMBOL_COLOR = "#475e69";
export function partStyle(p, color, faint = false) {
  return {
    color: faint ? "#77838b" : color,
    weight: p.role === "location" ? 4 : 2.8,
    opacity: faint ? .4 : 1,
    fillColor: faint ? "#77838b" : color,
    fillOpacity: faint ? .035 : p.role === "search_area" ? .07 : .2,
    // Hypothesis takes precedence, including for search areas.
    dashArray: p.hypo ? "8 5" : p.role === "search_area" ? "2 7" : null,
    lineCap: "round", lineJoin: "round",
  };
}

export function mapLegend({ terrain = false, context = true } = {}) {
  const svg = body => `<svg viewBox="0 0 38 22" width="38" height="22" aria-hidden="true">${body}</svg>`;
  const box = (fill, dash = "") => svg(`<rect x="5" y="4" width="28" height="14" rx="3" fill="${fill}" stroke="${SYMBOL_COLOR}" stroke-width="2.8" stroke-dasharray="${dash}"/>`);
  const line = (color, dash = "") => svg(`<path d="M4 11H34" stroke="white" stroke-width="6"/><path d="M4 11H34" stroke="${color}" stroke-width="2.5" stroke-dasharray="${dash}"/>`);
  return `<strong class="map-legend-title">Map key</strong><div class="map-legend-symbols">` +
    `<span>${box("#dce4e7")}Event extent</span>` +
    `<span>${svg(`<path d="M4 11H20" stroke="${SYMBOL_COLOR}" stroke-width="4"/><circle cx="29" cy="11" r="5" fill="${SYMBOL_COLOR}" stroke="white" stroke-width="2"/>`)}Location · line / point</span>` +
    `<span>${box("#f5f7f8", "2 7")}Search area · dotted</span>` +
    `<span>${box("#f5f7f8", "8 5")}Hypothesis · dashed</span>` +
    (context ? `<span>${line("#1971c2")}Streams</span><span>${line("#1d2433", "5 4")}Catchment</span>` : "") +
    `</div><p>Color identifies the run. A dashed outline marks a hypothesis for any geometry role.` +
    (terrain ? " Labels face you and stay the same size as you zoom." : "") + `</p>`;
}

const ringArea = ring => Math.abs(ring.reduce((a, p, i) => {
  const q = ring[(i + 1) % ring.length]; return a + p[0] * q[1] - q[0] * p[1];
}, 0));

const anchors = new WeakMap();
export function geometryAnchor(g) {
  if (!anchors.has(g)) anchors.set(g, computeAnchor(g));
  return anchors.get(g);
}

// Interior polygon anchor (including concavities and holes), or distance midpoint along a line.
function computeAnchor(g) {
  const c = g.coordinates;
  if (g.type === "Point") return c;
  if (g.type === "MultiPoint") return c[0];
  if (g.type.includes("LineString")) {
    const lines = g.type === "LineString" ? [c] : c;
    const length = line => line.slice(1).reduce((s, p, i) => s + Math.hypot(p[0] - line[i][0], p[1] - line[i][1]), 0);
    const line = [...lines].sort((a, b) => length(b) - length(a))[0];
    let remaining = length(line) / 2;
    for (let i = 1; i < line.length; i++) {
      const a = line[i - 1], b = line[i], d = Math.hypot(b[0] - a[0], b[1] - a[1]);
      if (d && remaining <= d) return [a[0] + (b[0] - a[0]) * remaining / d, a[1] + (b[1] - a[1]) * remaining / d];
      remaining -= d;
    }
    return line[0];
  }
  const rings = g.type === "MultiPolygon" ? [...c].sort((a, b) => ringArea(b[0]) - ringArea(a[0]))[0] : c;
  const outer = rings[0];
  let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
  outer.forEach(([x, y]) => { minX = Math.min(minX, x); minY = Math.min(minY, y); maxX = Math.max(maxX, x); maxY = Math.max(maxY, y); });
  const distance = ([x, y]) => {
    let inside = false, nearest = Infinity;
    for (const ring of rings) for (let i = 0, j = ring.length - 1; i < ring.length; j = i++) {
      const [ax, ay] = ring[j], [bx, by] = ring[i], dx = bx - ax, dy = by - ay;
      if ((ay > y) !== (by > y) && x < (bx - ax) * (y - ay) / (by - ay) + ax) inside = !inside;
      const t = Math.max(0, Math.min(1, ((x - ax) * dx + (y - ay) * dy) / (dx * dx + dy * dy || 1)));
      nearest = Math.min(nearest, Math.hypot(x - ax - t * dx, y - ay - t * dy));
    }
    return inside ? nearest : -nearest;
  };
  let best = outer[0], score = -Infinity, width = maxX - minX, height = maxY - minY;
  for (let round = 0; round < 5; round++) {
    for (let ix = 0; ix <= 8; ix++) for (let iy = 0; iy <= 8; iy++) {
      const point = [minX + width * ix / 8, minY + height * iy / 8], d = distance(point);
      if (d > score) { best = point; score = d; }
    }
    width /= 4; height /= 4; minX = best[0] - width / 2; minY = best[1] - height / 2;
  }
  return best;
}

// Fixed screen-size compass, shared by 2D maps and the rotating terrain camera.
export function northArrow(host) {
  const el = document.createElement("div");
  el.className = "map-north"; el.setAttribute("role", "img"); el.setAttribute("aria-label", "North");
  el.innerHTML = '<span>N</span><svg viewBox="0 0 16 20" width="14" height="18" aria-hidden="true"><path d="M8 2L14 18L8 14L2 18Z" fill="#263e36"/></svg>';
  host.append(el); return el;
}
export const EOX_URL = "https://tiles.maps.eox.at/wmts/1.0.0/s2cloudless-2025_3857/default/g/{z}/{y}/{x}.jpg";
export const EOX_ATTR = 'EOxCloudless <a href="https://cloudless.eox.at">EOX</a> · Contains modified Copernicus Sentinel data 2025 · <a href="https://creativecommons.org/licenses/by-nc-sa/4.0/">CC BY-NC-SA</a>';
