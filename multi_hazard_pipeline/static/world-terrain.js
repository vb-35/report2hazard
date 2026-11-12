/* Continuous Mapzen terrain. GeoJSON stays in WGS84 and drapes on streamed tiles. */
import { terrainControls, terrainMouseControls } from './terrain-controls.js';
import { partStyle, geometryAnchor, northArrow, EOX_URL, EOX_ATTR } from './map-display.js';
let library;
function loadLibrary() {
  if (!library) library = new Promise((resolve, reject) => {
    const css = document.createElement('link'); css.rel = 'stylesheet';
    css.href = 'https://cdn.jsdelivr.net/npm/maplibre-gl@5.24.0/dist/maplibre-gl.css'; document.head.append(css);
    const script = document.createElement('script');
    script.src = 'https://cdn.jsdelivr.net/npm/maplibre-gl@5.24.0/dist/maplibre-gl.js';
    script.onload = () => resolve(window.maplibregl);
    script.onerror = () => { library = null; reject(new Error('The world terrain viewer could not load. Check your connection or choose catchment surroundings.')); };
    document.head.append(script);
  });
  return library;
}
const bases = {
  osm: ['https://tile.openstreetmap.org/{z}/{x}/{y}.png', '© OpenStreetMap contributors', 19],
  eox: [EOX_URL, EOX_ATTR, 14],
  grau: ['https://mapsneu.wien.gv.at/basemap/bmapgrau/normal/google3857/{z}/{y}/{x}.png', '© basemap.at', 19],
  ortho: ['https://mapsneu.wien.gv.at/basemap/bmaporthofoto30cm/normal/google3857/{z}/{y}/{x}.jpeg', '© basemap.at', 19],
};
const collection = features => ({ type: 'FeatureCollection', features });
export async function mountWorldTerrain(container, data, options) {
  const ml = await loadLibrary();
  const view = new WorldTerrain(container, data, options, ml);
  try { await view.ready; return view; } catch (err) { view.dispose(); throw err; }
}
class WorldTerrain {
  constructor(container, data, options, ml) {
    this.data = data; this.options = options; this.ml = ml;
    this.run = options.run && data.runs[options.run] ? options.run : data.models[0].key;
    this.segments = new Set(options.segments ?? data.segments.map(s => s.id));
    this.labels = options.labels !== false; this.context = options.context !== false; this.others = !!options.others;
    this.basemap = options.basemap || 'osm'; this.exaggeration = options.exaggeration || 1.5;
    this.markers = []; this.disposed = false;
    container.replaceChildren(); const host = document.createElement('div'); host.className = 'world-terrain'; container.append(host);
    this.map = new ml.Map({ container: host, style: { version:8, sources:{}, layers:[{id:'background',type:'background',paint:{'background-color':'#dde6de'}}] }, center:[10,47], zoom:10, pitch:55, maxPitch:80, maxZoom:19, attributionControl:false });
    this.removeMouseControls = terrainMouseControls(this.map);
    this.navigation = terrainControls(container, action => this.navigate(action));
    this.compass = northArrow(container); this.compass.classList.add('map-north-3d');
    this.map.on('rotate', () => this.compass.style.setProperty('--north-bearing', `${-this.map.getBearing()}deg`));
    this.map.addControl(new ml.AttributionControl({compact:true}), 'bottom-right');
    this.map.addControl(new ml.NavigationControl({showCompass:false}), 'top-right');
    this.map.addControl(new ml.ScaleControl({maxWidth:120,unit:'metric'}), 'bottom-left');
    this.map.on('error', event => {
      options.notify?.(`Some map or elevation tiles could not load. Terrain may be incomplete here. ${event.error?.message || ''}`);
    });
    this.ready = new Promise((resolve, reject) => {
      this.readyReject = reject;
      this.loadTimer = setTimeout(() => reject(new Error('World terrain timed out. Check your connection and try again.')), 45000);
      this.map.on('load', () => {
        if (this.disposed) return;
        try {
          this.map.addSource('elevation', {type:'raster-dem',tiles:['https://elevation-tiles-prod.s3.amazonaws.com/terrarium/{z}/{x}/{y}.png'],tileSize:256,encoding:'terrarium',maxzoom:15,attribution:data.terrain.attribution});
          this.map.addLayer({id:'shade',type:'hillshade',source:'elevation',paint:{'hillshade-exaggeration':.35}});
          this.map.setTerrain({source:'elevation',exaggeration:this.exaggeration});
          this.map.addSource('parts', {type:'geojson',data:collection([])});
          this.map.addSource('context', {type:'geojson',data:collection([])});
          this.map.addLayer({id:'context-lines',type:'line',source:'context',paint:{'line-color':['get','color'],'line-width':1.5}});
          this.map.addLayer({id:'part-fill',type:'fill',source:'parts',filter:['==',['geometry-type'],'Polygon'],paint:{'fill-color':['get','color'],'fill-opacity':['get','fillOpacity']}});
          this.map.addLayer({id:'part-halo',type:'line',source:'parts',filter:['!=',['geometry-type'],'Point'],paint:{'line-color':'white','line-width':5,'line-opacity':['get','opacity']}});
          this.map.addLayer({id:'part-lines',type:'line',source:'parts',filter:['!=',['geometry-type'],'Point'],paint:{'line-color':['get','color'],'line-width':['get','weight'],'line-opacity':['get','opacity'],'line-dasharray':['case',['get','hypo'],['literal',[3,2]],['==',['get','role'],'search_area'],['literal',[1,3]],['literal',[1,0]]]}});
          this.map.addLayer({id:'part-points',type:'circle',source:'parts',filter:['==',['geometry-type'],'Point'],paint:{'circle-color':['get','color'],'circle-radius':6,'circle-stroke-width':2,'circle-stroke-color':'white'}});
          this.loaded = true; this.setBasemap(this.basemap); this.redraw(); this.resetView();
          this.observer = new ResizeObserver(() => this.map.resize()); this.observer.observe(container);
          clearTimeout(this.loadTimer); resolve();
        } catch (err) { clearTimeout(this.loadTimer); reject(err); }
      });
    });
  }
  redraw() {
    if (!this.loaded || this.disposed) return;
    const model = this.data.models.find(m => m.key === this.run);
    const features = this.data.runs[this.run].features.filter(f => this.segments.has(f.properties.seg) || this.others)
      .sort((a,b) => this.segments.has(a.properties.seg)-this.segments.has(b.properties.seg))
      .map(f => ({...f,properties:{...f.properties,...partStyle(f.properties,model.color,!this.segments.has(f.properties.seg))}}));
    this.map.getSource('parts').setData(collection(features));
    const context = this.context ? ['roads','streams','catchment'].flatMap(key => (this.data.context[key] || []).map(f => ({...f,properties:{color:key === 'streams' ? '#1971c2' : '#49565d'}}))) : [];
    this.map.getSource('context').setData(collection(context));
    this.markers.forEach(m => m.remove()); this.markers = [];
    if (this.labels) for (const f of features) {
      if (!this.segments.has(f.properties.seg)) continue;
      const el = document.createElement('span'); el.className = 'ptip'; el.style.cssText = 'background:white;padding:3px 6px;border:1px solid #a9b7bc;border-radius:5px;font:600 12px system-ui';
      el.textContent = f.properties.part || f.properties.seg; el.title = f.properties.label || '';
      this.markers.push(new this.ml.Marker({element:el,anchor:'bottom',offset:[0,-5]}).setLngLat(geometryAnchor(f.geometry)).addTo(this.map));
    }
  }
  setBasemap(kind) {
    this.basemap = kind; if (!this.loaded) return;
    if (this.map.getLayer('basemap')) this.map.removeLayer('basemap');
    if (this.map.getSource('basemap')) this.map.removeSource('basemap');
    if (bases[kind]) {
      const [url,attribution,maxzoom] = bases[kind];
      this.map.addSource('basemap',{type:'raster',tiles:[url],tileSize:256,maxzoom,attribution});
      this.map.addLayer({id:'basemap',type:'raster',source:'basemap'},'shade');
      if (kind === 'ortho' || kind === 'grau') this.options.notify?.('basemap.at imagery covers Austria. Choose OpenStreetMap or EOxCloudless when exploring other regions; elevation continues independently.');
    }
  }
  setRun(key) { if (this.data.runs[key]) { this.run=key; this.redraw(); } }
  setSegments(ids) { this.segments=new Set(ids); this.redraw(); }
  setExaggeration(value) { this.exaggeration=Number(value)||1; if(this.loaded) this.map.setTerrain({source:'elevation',exaggeration:this.exaggeration}); }
  setOptions(options) { for (const key of ['labels','context','others']) if(options[key] !== undefined) this[key]=options[key]; this.redraw(); }
  navigate(action) {
    if (action === 'reset') return this.resetView();
    if (action === 'north') return this.map.jumpTo({bearing:0});
    if (action.startsWith('pan-')) {
      const offsets = {'pan-left':[-80,0],'pan-right':[80,0],'pan-up':[0,-80],'pan-down':[0,80]};
      return this.map.panBy(offsets[action],{duration:0});
    }
    const bearing=this.map.getBearing(), pitch=this.map.getPitch(), zoom=this.map.getZoom();
    if(action === 'rotate-left') this.map.jumpTo({bearing:bearing-15});
    if(action === 'rotate-right') this.map.jumpTo({bearing:bearing+15});
    if(action === 'tilt-up') this.map.jumpTo({pitch:Math.min(80,pitch+8)});
    if(action === 'tilt-down') this.map.jumpTo({pitch:Math.max(0,pitch-8)});
    if(action === 'zoom-in') this.map.jumpTo({zoom:Math.min(19,zoom+.5)});
    if(action === 'zoom-out') this.map.jumpTo({zoom:Math.max(0,zoom-.5)});
  }
  resetView() {
    let bounds = this.data.bounds;
    if (!bounds) {
      const points = Object.values(this.data.runs).flatMap(r=>r.features.map(f=>geometryAnchor(f.geometry)));
      if(points.length) bounds = [[Math.min(...points.map(p=>p[1])),Math.min(...points.map(p=>p[0]))],[Math.max(...points.map(p=>p[1])),Math.max(...points.map(p=>p[0]))]];
    }
    if(bounds) this.map.fitBounds([[bounds[0][1],bounds[0][0]],[bounds[1][1],bounds[1][0]]],{padding:60,pitch:55,bearing:0,duration:0,maxZoom:15});
  }
  dispose() { this.disposed=true; clearTimeout(this.loadTimer); this.observer?.disconnect(); this.markers.forEach(m=>m.remove()); this.removeMouseControls(); this.navigation.remove(); this.map.remove(); this.compass.remove(); }
}
