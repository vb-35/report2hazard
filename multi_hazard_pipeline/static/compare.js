/* Compare localizations: one synced map per run (or one overlay map), a segment list and a comparison table. */
import { partStyle, mapLegend, geometryAnchor, northArrow, EOX_URL, EOX_ATTR } from "./map-display.js";
(() => {
  const root = document.getElementById("cmp");
  if (!root) return;
  const $ = id => document.getElementById(id);
  const esc = s => String(s ?? "").replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  const ROLE = { event_extent: "extent", location: "location", search_area: "search area" };
  let DATA, REP, S = { view: "side", segments: new Set(), visible: new Set(), run3d: null }, maps = [], syncing = false, view3d = null, token3d = 0;
  const selected = f => S.segments.has(f.properties.seg);

  const model = k => DATA.models.find(m => m.key === k);
  const runRef = m => `R${DATA.models.indexOf(m) + 1}`;
  const partsOf = (run, seg) => run.features.filter(f => f.properties.seg === seg);
  const statusOf = (run, seg) => {
    const r = run.results[seg], n = partsOf(run, seg).length;
    if (!r) return n ? "located" : "missing";
    return /unres/i.test(r.status) || (!n && !/loc/i.test(r.status)) ? "unresolved" : "located";
  };

  function tiles(kind) {
    const at = 'Grundkarte: <a href="https://basemap.at/">basemap.at</a>';
    if (kind === "eox") return L.tileLayer(EOX_URL, { maxNativeZoom: 14, maxZoom: 19, attribution: EOX_ATTR });
    if (kind === "osm") return L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", { maxZoom: 19, attribution: "&copy; OpenStreetMap contributors" });
    if (kind === "grau") return L.tileLayer("https://mapsneu.wien.gv.at/basemap/bmapgrau/normal/google3857/{z}/{y}/{x}.png", { maxZoom: 19, attribution: at });
    if (kind === "ortho") return L.tileLayer("https://mapsneu.wien.gv.at/basemap/bmaporthofoto30cm/normal/google3857/{z}/{y}/{x}.jpeg", { maxZoom: 19, attribution: at });
    return null;
  }

  function style(f, color, faint) {
    return partStyle(f.properties, color, faint);
  }

  function popup(f, m) {
    const p = f.properties;
    return `<b style="color:${m.color}">${runRef(m)}</b><br><b>${esc(p.part)}</b> ${esc(p.label)}<br>` +
      `${esc(ROLE[p.role] || p.role)}${p.hypo ? " · hypothesis" : ""}<br>` +
      `event ${esc(p.ec || "–")} · location ${esc(p.lc || "–")} · boundary ${esc(p.bc || "–")}` +
      (p.area ? `<br>area ${esc(p.area)} ha${p.report_area ? ` (report ${esc(p.report_area)} ha)` : ""}` : "") +
      (p.method ? `<br><i>${esc(p.method.slice(0, 220))}</i>` : "");
  }

  function contextLayer() {
    const c = REP.context, g = L.layerGroup();
    L.geoJSON(c.catchment, { style: { color: "#1d2433", weight: 1.5, dashArray: "5 4", fill: false, opacity: .7 }, interactive: false }).addTo(g);
    L.geoJSON(c.roads, { style: { color: "#868e96", weight: 1.2, opacity: .7 }, interactive: false }).addTo(g);
    L.geoJSON(c.streams, { style: { color: "#1971c2", weight: 1.6, opacity: .85 },
      onEachFeature: (f, l) => f.properties.name && l.bindTooltip(esc(f.properties.name), { sticky: true }) }).addTo(g);
    L.geoJSON(c.places, { pointToLayer: (f, ll) => L.circleMarker(ll, { radius: 2.5, color: "#5b6475", weight: 1, fillOpacity: .6 }),
      onEachFeature: (f, l) => f.properties.name && l.bindTooltip(esc(f.properties.name)) }).addTo(g);
    return g;
  }

  function partsLayer(keys) {
    const g = L.featureGroup(), others = $("others").checked, labels = $("labels").checked;
    for (const k of keys) {
      const m = model(k), run = REP.runs[k];
      for (const f of run.features) {
        const mine = selected(f);
        if (!mine && !others) continue;
        const symbol = style(f, m.color, !mine);
        // A white casing separates symbols from both light maps and dark aerial imagery.
        L.geoJSON(f, {
          style: { ...symbol, color: "#fff", weight: symbol.weight + 3, fill: false, opacity: mine ? .95 : .35, interactive: false },
          pointToLayer: (ft, ll) => L.circleMarker(ll, { radius: 8, color: "#fff", weight: 2, fill: false, interactive: false }),
          interactive: false,
        }).addTo(g);
        const lay = L.geoJSON(f, {
          style: ft => style(ft, m.color, !mine),
          pointToLayer: (ft, ll) => L.circleMarker(ll, { radius: 6, ...symbol, weight: 2.5, fillOpacity: mine ? .9 : .1 }),
        });
        lay.bindPopup(popup(f, m));
        lay.on("click", () => { if (!selected(f)) selectSeg(f.properties.seg, false); });
        if (mine && labels) {
          const [lon, lat] = geometryAnchor(f.geometry);
          const text = `${keys.length > 1 ? `R${DATA.models.indexOf(m) + 1} · ` : ""}${f.properties.part || f.properties.seg}`;
          L.tooltip({ permanent: true, direction: "top", className: "ptip", offset: [0, -4] })
            .setLatLng([lat, lon]).setContent(esc(text)).addTo(g);
        }
        lay.addTo(g);
      }
    }
    return g;
  }

  function segBounds() {
    const b = L.latLngBounds([]);
    for (const k of S.visible) for (const f of REP.runs[k].features)
      if (selected(f)) { try { b.extend(L.geoJSON(f).getBounds()); } catch (e) { /* empty geometry */ } }
    if (b.isValid()) return b.pad(.15);
    return REP.bounds ? L.latLngBounds(REP.bounds).pad(.1) : null;
  }

  function layoutLabels(entry) {
    if (!entry.parts) return;
    const occupied = [], bounds = entry.map.getContainer().getBoundingClientRect();
    entry.parts.eachLayer(layer => {
      if (!(layer instanceof L.Tooltip)) return;
      const el = layer.getElement();
      if (!el) return;
      el.style.visibility = "";
      let placed = false;
      for (const offset of [[0, -4], [0, -32], [0, 24], [-45, -4], [45, -4]]) {
        layer.options.offset = L.point(offset); layer.update();
        const b = el.getBoundingClientRect();
        if (b.left < bounds.left || b.right > bounds.right || b.top < bounds.top || b.bottom > bounds.bottom) continue;
        if (occupied.every(o => b.right + 3 < o.left || b.left > o.right + 3 || b.bottom + 3 < o.top || b.top > o.bottom + 3)) {
          occupied.push(b); placed = true; break;
        }
      }
      // At crowded scales, reveal additional labels as the user zooms in.
      el.style.visibility = placed ? "" : "hidden";
    });
  }

  /* 3D: one run at a time on the report's terrain, with a switch between the runs. */
  function update3dStat() {
    const m = model(S.run3d), stat = $("stat3d");
    if (stat && m) stat.innerHTML = `<span class="sw" style="background:${m.color}"></span> ${REP.runs[S.run3d].features.filter(selected).length} selected parts`;
  }

  function build3d() {
    const box = $("maps"), token = ++token3d;
    box.className = "cmp-maps overlay";
    S.run3d = S.run3d && DATA.models.some(m => m.key === S.run3d) ? S.run3d : DATA.models[0].key;
    const card = document.createElement("div"); card.className = "card";
    card.innerHTML = `<div class="hd"><span class="name">3D view</span>
      <label class="cmp-lbl">Run <select id="run3d">${DATA.models.map((m, i) => `<option value="${esc(m.key)}" ${m.key === S.run3d ? "selected" : ""}>${runRef(m)}</option>`).join("")}</select></label>
      <label class="cmp-lbl">Terrain exaggeration<input id="exag3d" type="range" min="1" max="3" step="0.25" value="1.5"></label>
      <button type="button" class="cmp-tab" id="reset3d">Reset view</button>
      <span class="stat" id="stat3d"></span>
      <span class="sub">Drag to rotate, right-drag to pan, scroll to zoom. Choose a terrain source below. Terrain is used for viewing only.</span></div>
      <div id="terrain-sources"></div><div class="terrain" id="terrain3d"><p class="cmp-loading">Loading the terrain…</p></div>`;
    box.append(card);
    update3dStat();
    import(root.dataset.terrainModule).then(mod => {
      if (token !== token3d) return;
      mod.terrainSources($("terrain-sources"), root.dataset.terrainUrl, async options => {
        if (token !== token3d) throw new Error("View changed.");
        view3d = null;
        $("terrain3d").textContent = "Loading terrain...";
        try {
          const view = await mod.mount($("terrain3d"), { ...options, run: S.run3d, segments: [...S.segments],
            basemap: $("basemap").value, labels: $("labels").checked, context: $("ctx").checked, others: $("others").checked });
          if (token !== token3d) { view.dispose(); throw new Error("View changed."); }
          view3d = view;
          $("exag3d").value = "1.5";
          $("run3d").onchange = e => { S.run3d = e.target.value; view.setRun(S.run3d); update3dStat(); };
          $("exag3d").oninput = e => view.setExaggeration(e.target.value);
          $("reset3d").onclick = () => view.resetView();
          return view;
        } catch (err) {
          if (token === token3d) { view3d = null; $("terrain3d").textContent = "Choose a terrain source above to load the 3D view."; }
          throw err;
        }
      });
    }).catch(err => { if (token === token3d) $("terrain3d").textContent = err.message || String(err); });
  }

  function buildMaps() {
    maps.forEach(x => x.map.remove()); maps = [];
    if (view3d) { view3d.dispose(); view3d = null; }
    token3d++;
    const box = $("maps"); box.innerHTML = ""; box.className = "cmp-maps" + (S.view === "overlay" ? " overlay" : "");
    if (S.view === "3d") { build3d(); return; }
    const panes = S.view === "overlay" ? [{ keys: DATA.models.map(m => m.key) }] : DATA.models.map(m => ({ keys: [m.key], m }));
    for (const pane of panes) {
      const card = document.createElement("div"); card.className = "card";
      const hd = document.createElement("div"); hd.className = "hd";
      hd.innerHTML = pane.m
        ? `<span class="sw" style="background:${pane.m.color}"></span><span class="name">${runRef(pane.m)}</span><span class="stat"></span>`
        : `<span class="name">All runs</span><span class="stat"></span>`;
      const el = document.createElement("div"); el.className = "map";
      card.append(hd, el);
      if (!pane.m) {
        const lg = document.createElement("div"); lg.className = "legend";
        lg.innerHTML = DATA.models.map((m, i) => `<label><input type="checkbox" data-k="${esc(m.key)}" ${S.visible.has(m.key) ? "checked" : ""}>` +
          `<span class="sw" style="background:${m.color}"></span>${runRef(m)}</label>`).join("");
        lg.addEventListener("change", e => { const k = e.target.dataset.k; e.target.checked ? S.visible.add(k) : S.visible.delete(k); refreshLayers(false); });
        card.append(lg);
      }
      box.append(card);
      const map = L.map(el, { zoomSnap: .25 });
      const entry = { map, keys: pane.keys, hd, m: pane.m, base: null, ctx: null, parts: null };
      maps.push(entry);
      map.on("zoomend moveend resize", () => layoutLabels(entry));
      map.on("move zoom", () => {
        if (syncing) return; syncing = true;
        maps.forEach(o => o.map !== map && o.map.setView(map.getCenter(), map.getZoom(), { animate: false }));
        syncing = false;
      });
    }
    northArrow(box);
    refreshBase(); refreshLayers(true);
  }

  function refreshBase() {
    const kind = $("basemap").value;
    for (const e of maps) { if (e.base) e.map.removeLayer(e.base); e.base = tiles(kind); if (e.base) e.base.addTo(e.map); }
  }

  function refreshLayers(fit) {
    const showCtx = $("ctx").checked;
    for (const e of maps) {
      if (e.ctx) e.map.removeLayer(e.ctx);
      if (e.parts) e.map.removeLayer(e.parts);
      e.ctx = showCtx ? contextLayer().addTo(e.map) : null;
      e.parts = partsLayer(e.m ? e.keys : e.keys.filter(k => S.visible.has(k))).addTo(e.map);
      const stat = e.hd.querySelector(".stat");
      if (!e.m) { stat.textContent = `${S.segments.size} of ${REP.segments.length} segments selected`; continue; }
      const run = REP.runs[e.m.key];
      const sts = REP.segments.filter(s => S.segments.has(s.id)).map(s => statusOf(run, s.id));
      stat.textContent = `${sts.filter(s => s === "located").length} located · ${sts.filter(s => s !== "located").length} unresolved · ${run.features.filter(selected).length} parts`;
    }
    if (fit && maps.length) {
      const b = segBounds();
      if (b) { syncing = true; maps.forEach(e => e.map.fitBounds(b, { animate: false })); syncing = false; }
    }
    setTimeout(() => maps.forEach(e => { e.map.invalidateSize(); layoutLabels(e); }), 50);
  }

  function dotsFor(seg) {
    return DATA.models.map((m, i) => {
      const run = REP.runs[m.key], st = statusOf(run, seg), n = partsOf(run, seg).length;
      const description = `${runRef(m)}: ${st === "located" ? `${n} mapped part${n === 1 ? "" : "s"}` : st}`;
      return `<span class="part-count ${st === "located" ? "" : "unres"}" title="${esc(description)}" aria-label="${esc(description)}" style="--run-color:${m.color}"><span>R${i + 1}</span><b>${st === "located" ? n : "—"}</b></span>`;
    }).join("");
  }

  function buildList() {
    const a = $("segList");
    a.innerHTML = `<div class="segment-selection"><strong>Segments</strong><span class="selection-count" role="status" aria-live="polite">${S.segments.size} / ${REP.segments.length} selected</span>` +
      `<div class="selection-actions"><button type="button" class="cmp-tab" data-selection="all">Select all</button><button type="button" class="cmp-tab" data-selection="none">Clear</button></div>` +
      `<p>Check any combination to show on the maps.</p><p>Parts per run: R1, R2… identify runs; numbers count mapped parts; — means unresolved / missing.</p><details class="run-key"><summary>Run colors</summary>` +
      DATA.models.map((m, i) => `<div><span class="sw" style="background:${m.color}"></span><b class="run-ref">R${i + 1}</b></div>`).join("") +
      `<p>Full run details are in the run list above.</p></details></div>` +
      REP.segments.map(s => `<label class="item ${S.segments.has(s.id) ? "active" : ""}" data-seg="${esc(s.id)}">` +
        `<input type="checkbox" aria-label="Select segment ${esc(s.id)}" ${S.segments.has(s.id) ? "checked" : ""}>` +
        `<span class="segment-content"><span class="t">Segment ${esc(s.id)} <span class="segment-category">· ${esc(s.category)}</span></span>` +
        `<span class="p">${esc(s.process)}</span><span class="dots">${dotsFor(s.id)}</span></span></label>`).join("");
    a.querySelectorAll(".item input").forEach(el => {
      el.addEventListener("change", () => selectSeg(el.closest(".item").dataset.seg, true));
    });
    a.querySelectorAll("[data-selection]").forEach(el => el.addEventListener("click", () => {
      S.segments = new Set(el.dataset.selection === "all" ? REP.segments.map(s => s.id) : []);
      updateSelection();
    }));
  }

  function compareSeg(seg) {
    const s = REP.segments.find(x => x.id === seg) || { process: "", category: "", interaction: "", phase: "" };
    const cards = DATA.models.map(m => {
      const run = REP.runs[m.key], st = statusOf(run, seg), parts = partsOf(run, seg), r = run.results[seg] || {};
      const partsHtml = parts.map(f => {
        const p = f.properties;
        const more = (label, text) => text ? `<details><summary>${label}</summary><div class="txt">${esc(text)}</div></details>` : "";
        return `<div class="part"><div class="pl">${esc(p.part)} ${esc(p.label)}</div>` +
          `<div><span class="badge">${esc(ROLE[p.role] || p.role)}</span>${p.hypo ? '<span class="badge hypo">hypothesis</span>' : ""}` +
          (p.area ? `<span class="badge">${esc(p.area)} ha${p.report_area ? " / report " + esc(p.report_area) : ""}</span>` : "") + `</div>` +
          `<div class="meta">event ${esc(p.ec || "–")} · location ${esc(p.lc || "–")} · boundary ${esc(p.bc || "–")}</div>` +
          (p.method ? `<div class="meta">${esc(p.method)}</div>` : "") +
          more("Reasoning", p.reasoning) + more("Uncertainty", p.uncertainty) + more("Alternatives", p.alternatives) +
          more("Quotes", p.quotes.replace(/\s*(\|\||\|)\s*/g, "\n\n")) + `</div>`;
      }).join("");
      return `<div class="mcard" style="--c:${m.color}"><h3><span class="sw" style="background:${m.color}"></span>${runRef(m)}` +
        `<span class="badge ${st === "located" ? "ok" : "bad"}">${st}${st === "located" ? " · " + parts.length : ""}</span></h3>` +
        (r.reason ? `<div class="reason">${esc(r.reason)}</div>` : "") + partsHtml + `</div>`;
    }).join("");
    return `<h2>Segment ${esc(seg)}</h2><div class="seginfo"><b>${esc(s.process)}</b><br>${esc(s.category)} · ${esc(s.interaction)} · ${esc(s.phase)}</div><div class="cols">${cards}</div>`;
  }

  function overview() {
    const segs = REP.segments;
    const head = `<tr><th>Run</th><th>Located</th><th>Unresolved</th><th>Parts</th><th>Extents</th><th>Hypotheses</th>` +
      segs.map(s => `<th class="c">${esc(s.id)}</th>`).join("") + `</tr>`;
    const rows = DATA.models.map(m => {
      const run = REP.runs[m.key], st = segs.map(s => statusOf(run, s.id)), fs = run.features;
      return `<tr><td><span class="sw" style="background:${m.color}"></span> ${runRef(m)}</td>` +
        `<td class="c">${st.filter(x => x === "located").length}</td><td class="c">${st.filter(x => x !== "located").length}</td>` +
        `<td class="c">${fs.length}</td><td class="c">${fs.filter(f => f.properties.role === "event_extent").length}</td>` +
        `<td class="c">${fs.filter(f => f.properties.hypo).length}</td>` +
        segs.map((s, i) => `<td class="c">${st[i] === "located" ? `<span class="cell" style="background:${m.color}">${partsOf(run, s.id).length}</span>`
          : `<span class="cell unres" style="border-color:${m.color}">–</span>`}</td>`).join("") + `</tr>`;
    }).join("");
    const segRows = segs.map(s => `<tr class="segrow" data-seg="${esc(s.id)}"><td class="c"><b>${esc(s.id)}</b></td><td>${esc(s.process)}</td>` +
      `<td>${esc(s.category)}</td><td><div class="dots">${dotsFor(s.id)}</div></td></tr>`).join("");
    return `<h2>${esc(REP.name)}: overview</h2><div class="seginfo">Numbers are mapped parts per segment; a dash means unresolved.</div>` +
      `<div class="tablewrap"><table class="matrix">${head}${rows}</table></div>` +
      `<h2 style="margin-top:14px">Segments</h2><div class="tablewrap"><table class="matrix"><tr><th>#</th><th>Process</th><th>Category</th><th>Parts per run</th></tr>${segRows}</table></div>`;
  }

  function buildCompare() {
    const c = $("compare");
    const ids = REP.segments.filter(s => S.segments.has(s.id)).map(s => s.id);
    c.innerHTML = !ids.length ? '<div class="selection-empty">No segments selected. Check a segment or choose Select all to display results.</div>' :
      ids.length === REP.segments.length ? overview() : ids.map(compareSeg).join("");
    c.querySelectorAll("tr.segrow").forEach(tr => tr.addEventListener("click", () => selectSeg(tr.dataset.seg, true)));
  }

  function selectSeg(seg, fromList) {
    S.segments.has(seg) ? S.segments.delete(seg) : S.segments.add(seg);
    updateSelection(seg);
  }

  function updateSelection(focusSeg) {
    const focused = document.activeElement;
    const restoreFocus = $("segList").contains(focused);
    const action = focused?.dataset.selection;
    const keyOpen = $("segList").querySelector(".run-key")?.open;
    buildList(); buildCompare(); refreshLayers(true);
    $("segList").querySelector(".run-key").open = keyOpen;
    if (restoreFocus) {
      const target = action ? $("segList").querySelector(`[data-selection="${action}"]`) :
        [...$("segList").querySelectorAll(".item")].find(el => el.dataset.seg === focusSeg)?.querySelector("input");
      target?.focus({ preventScroll: true });
    }
    if (view3d) view3d.setSegments([...S.segments]);
    update3dStat();
  }

  function keys() {
    $("keys").innerHTML = mapLegend({ terrain: S.view === "3d", context: $("ctx").checked });
  }

  $("viewTabs").addEventListener("click", e => {
    const v = e.target.dataset.view; if (!v) return; S.view = v;
    document.querySelectorAll("#viewTabs .cmp-tab").forEach(b => b.setAttribute("aria-pressed", String(b.dataset.view === v)));
    buildMaps(); keys();
  });
  $("basemap").addEventListener("change", () => { refreshBase(); if (view3d) view3d.setBasemap($("basemap").value); });
  ["ctx", "others", "labels"].forEach(id => $(id).addEventListener("change", () => {
    refreshLayers(false); keys();
    if (view3d) view3d.setOptions({ context: $("ctx").checked, others: $("others").checked, labels: $("labels").checked });
  }));

  fetch(root.dataset.url, { headers: { Accept: "application/json" } })
    .then(r => r.json().then(body => ({ ok: r.ok, body })))
    .then(({ ok, body }) => {
      if (!ok) throw new Error(body.error || "The runs could not be loaded.");
      DATA = body; REP = DATA.reports[0]; S.visible = new Set(DATA.models.map(m => m.key));
      S.segments = new Set(REP.segments.map(s => s.id));
      $("cmpStatus").hidden = true; $("cmpBody").hidden = false;
      keys(); buildList(); buildCompare(); buildMaps();
    })
    .catch(err => { $("cmpStatus").textContent = err.message; $("cmpStatus").classList.add("message", "error"); });
})();
